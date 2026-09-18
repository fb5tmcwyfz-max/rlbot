// language: C, file: wk_driver.c, target: Windows 11 23H2 (build 22631), WDK
// Self-signed kernel driver. Drop-in replacement for WdiSvcMon — same device
// name, same IOCTL table, same struct layouts as pk_pymem.py / mem.py / kwrite.py.

#include "wk_driver.h"
#include <ntimage.h>
#include <ntstrsafe.h>
#include <aux_klib.h>

// Enum ZwQuerySystemInformation — non déclaré dans les headers WDK par défaut
typedef enum _SYSTEM_INFORMATION_CLASS {
    SystemBasicInformation = 0,
    SystemProcessInformation = 5,
    SystemModuleInformation = 11,
} SYSTEM_INFORMATION_CLASS;

NTSTATUS ZwQuerySystemInformation(
    SYSTEM_INFORMATION_CLASS SystemInformationClass,
    PVOID SystemInformation,
    ULONG SystemInformationLength,
    PULONG ReturnLength
);

// --- Manual declaration of NtQuerySystemInformation ---
// The SYSTEM_INFORMATION_CLASS enum is not exposed in kernel mode on modern
// WDKs. We declare the function ourselves with a ULONG class parameter, and
// pass the numeric values directly (5 = SystemProcessInformation).
NTSYSAPI NTSTATUS NTAPI NtQuerySystemInformation(
    ULONG SystemInformationClass,
    PVOID SystemInformation,
    ULONG SystemInformationLength,
    PULONG ReturnLength
);

#define MY_SystemProcessInformation 5

// ==================================================================
// Globals
// ==================================================================

static PDEVICE_OBJECT   g_Device = NULL;
static KSPIN_LOCK       g_KwriteLock;
static KSPIN_LOCK       g_AutowriteLock;
static KWRITE_SLOT      g_KwriteSlots[KWRITE_SLOTS_MAX];
static AUTOWRITE_SLOT   g_AutowriteSlots[AUTOWRITE_SLOTS_MAX];
static PETHREAD         g_ScrubThread = NULL;
static PETHREAD         g_AutowriteThread = NULL;
static BOOLEAN          g_Running = TRUE;

static PVOID            g_PiDdbCacheTable = NULL;
static PVOID            g_KernelHashBucketList = NULL;
static PVOID            g_NtoskrnlBase = NULL;
static ULONG            g_NtoskrnlSize = 0;

// ==================================================================
// Small helpers
// ==================================================================

static PEPROCESS lookup_process(ULONG64 pid)
{
    PEPROCESS proc = NULL;
    if (NT_SUCCESS(PsLookupProcessByProcessId((HANDLE)(ULONG_PTR)pid, &proc)))
        return proc;
    return NULL;
}

static NTSTATUS do_read(ULONG64 pid, ULONG64 addr, PVOID out, ULONG32 size)
{
    if (size == 0) return STATUS_INVALID_PARAMETER;
    if (addr < 0x10000) return STATUS_INVALID_ADDRESS;

    PEPROCESS src = lookup_process(pid);
    if (!src) return STATUS_NOT_FOUND;

    SIZE_T copied = 0;
    NTSTATUS st = MmCopyVirtualMemory(src, (PVOID)(ULONG_PTR)addr,
                                      PsGetCurrentProcess(), out, (SIZE_T)size,
                                      UserMode, &copied);
    ObDereferenceObject(src);
    return st;
}

static NTSTATUS do_write(ULONG64 pid, ULONG64 addr, PVOID in, ULONG32 size)
{
    if (size == 0) return STATUS_INVALID_PARAMETER;
    if (addr < 0x10000) return STATUS_INVALID_ADDRESS;

    PEPROCESS dst = lookup_process(pid);
    if (!dst) return STATUS_NOT_FOUND;

    SIZE_T copied = 0;
    NTSTATUS st = MmCopyVirtualMemory(PsGetCurrentProcess(), in,
                                      dst, (PVOID)(ULONG_PTR)addr, (SIZE_T)size,
                                      UserMode, &copied);
    ObDereferenceObject(dst);
    return st;
}

// ==================================================================
// Kernel-side process lookup — NtQuerySystemInformation.
// ==================================================================

typedef struct _SPI {
    ULONG         NextEntryOffset;
    ULONG         NumberOfThreads;
    LARGE_INTEGER WorkingSetPrivateSize;
    ULONG         HardFaultCount;
    ULONG         NumberOfThreadsHighWatermark;
    ULONGLONG     CycleTime;
    LARGE_INTEGER CreateTime;
    LARGE_INTEGER UserTime;
    LARGE_INTEGER KernelTime;
    UNICODE_STRING ImageName;
    KPRIORITY     BasePriority;
    HANDLE        UniqueProcessId;
    HANDLE        InheritedFromUniqueProcessId;
    ULONG         HandleCount;
    ULONG         SessionId;
    ULONG_PTR     UniqueProcessKey;
    SIZE_T        PeakVirtualSize;
    SIZE_T        VirtualSize;
    ULONG         PageFaultCount;
    SIZE_T        PeakWorkingSetSize;
    SIZE_T        WorkingSetSize;
    SIZE_T        QuotaPeakPagedPoolUsage;
    SIZE_T        QuotaPagedPoolUsage;
    SIZE_T        QuotaPeakNonPagedPoolUsage;
    SIZE_T        QuotaNonPagedPoolUsage;
    SIZE_T        PagefileUsage;
    SIZE_T        PeakPagefileUsage;
    SIZE_T        PrivatePageCount;
    LARGE_INTEGER ReadOperationCount;
    LARGE_INTEGER WriteOperationCount;
    LARGE_INTEGER OtherOperationCount;
    LARGE_INTEGER ReadTransferCount;
    LARGE_INTEGER WriteTransferCount;
    LARGE_INTEGER OtherTransferCount;
} SPI, *PSPI;

static NTSTATUS find_pid_by_name(PCWSTR name, PULONG32 pidOut)
{
    *pidOut = 0;
    UNICODE_STRING needle;
    RtlInitUnicodeString(&needle, name);

    ULONG needed = 0;
    NTSTATUS st = NtQuerySystemInformation(MY_SystemProcessInformation,
                                           NULL, 0, &needed);
    if (st != STATUS_INFO_LENGTH_MISMATCH && !NT_SUCCESS(st)) return st;

    PVOID buf = ExAllocatePool2(POOL_FLAG_NON_PAGED, needed + PAGE_SIZE, POOL_TAG);
    if (!buf) return STATUS_INSUFFICIENT_RESOURCES;

    st = NtQuerySystemInformation(MY_SystemProcessInformation, buf, needed, &needed);
    if (!NT_SUCCESS(st)) { ExFreePoolWithTag(buf, POOL_TAG); return st; }

    PSPI pi = (PSPI)buf;
    for (;;) {
        if (pi->ImageName.Buffer && pi->ImageName.Length > 0) {
            if (RtlEqualUnicodeString(&pi->ImageName, &needle, TRUE)) {
                *pidOut = (ULONG32)(ULONG_PTR)pi->UniqueProcessId;
                break;
            }
        }
        if (pi->NextEntryOffset == 0) break;
        pi = (PSPI)((PUCHAR)pi + pi->NextEntryOffset);
    }

    ExFreePoolWithTag(buf, POOL_TAG);
    return (*pidOut != 0) ? STATUS_SUCCESS : STATUS_NOT_FOUND;
}

// ==================================================================
// Module info — AuxKlib
// ==================================================================

static NTSTATUS get_module_info(PVOID* ntBase, ULONG* ntSize,
                                 PVOID* ciBase, ULONG* ciSize)
{
    *ntBase = *ciBase = NULL;
    *ntSize = *ciSize = 0;

    ULONG needed = 0;
    NTSTATUS st = AuxKlibQueryModuleInformation(&needed,
                                                 sizeof(AUX_MODULE_EXTENDED_INFO),
                                                 NULL);
    if (!NT_SUCCESS(st)) return st;
    if (needed == 0) return STATUS_NOT_FOUND;

    PVOID buf = ExAllocatePool2(POOL_FLAG_NON_PAGED, needed + PAGE_SIZE, POOL_TAG);
    if (!buf) return STATUS_INSUFFICIENT_RESOURCES;

    st = AuxKlibQueryModuleInformation(&needed,
                                        sizeof(AUX_MODULE_EXTENDED_INFO),
                                        buf);
    if (!NT_SUCCESS(st)) { ExFreePoolWithTag(buf, POOL_TAG); return st; }

    PAUX_MODULE_EXTENDED_INFO mods = (PAUX_MODULE_EXTENDED_INFO)buf;
    ULONG n = needed / sizeof(AUX_MODULE_EXTENDED_INFO);
    for (ULONG i = 0; i < n; i++) {
        const char* fname = (const char*)mods[i].FullPathName + mods[i].FileNameOffset;
        if (_stricmp(fname, "ntoskrnl.exe") == 0) {
            *ntBase = mods[i].BasicInfo.ImageBase;
            *ntSize = mods[i].ImageSize;
        } else if (_stricmp(fname, "ci.dll") == 0) {
            *ciBase = mods[i].BasicInfo.ImageBase;
            *ciSize = mods[i].ImageSize;
        }
    }
    ExFreePoolWithTag(buf, POOL_TAG);
    return STATUS_SUCCESS;
}

// ==================================================================
// Pattern scanner
// ==================================================================

static PUCHAR pattern_scan(PUCHAR base, ULONG size, const UCHAR* pat,
                            const char* mask, ULONG patLen)
{
    if (size < patLen) return NULL;
    for (ULONG i = 0; i <= size - patLen; i++) {
        BOOLEAN hit = TRUE;
        for (ULONG j = 0; j < patLen; j++) {
            if (mask[j] == 'x' && base[i + j] != pat[j]) { hit = FALSE; break; }
        }
        if (hit) return base + i;
    }
    return NULL;
}

static PVOID resolve_rip_rel(PUCHAR instr)
{
    LONG disp = *(LONG*)(instr + 3);
    return (PVOID)(instr + 7 + disp);
}

static PVOID locate_piddb_cache(PUCHAR base, ULONG size)
{
    static const UCHAR pat[] = {
        0x48, 0x8D, 0x0D, 0x00, 0x00, 0x00, 0x00,
        0xE8, 0x00, 0x00, 0x00, 0x00,
        0x48, 0x8B, 0x0D, 0x00, 0x00, 0x00, 0x00
    };
    static const char mask[] = "xxx????x????xxx????";

    PUCHAR p = pattern_scan(base, size, pat, mask, sizeof(pat));
    while (p) {
        PVOID target = resolve_rip_rel(p + 12);
        if ((PUCHAR)target >= base && (PUCHAR)target < base + size)
            return target;
        p = pattern_scan(p + 1, size - (ULONG)(p + 1 - base), pat, mask, sizeof(pat));
    }
    return NULL;
}

static PVOID locate_hash_bucket_list(PUCHAR base, ULONG size)
{
    static const UCHAR pat[] = {
        0x48, 0x8B, 0x0D, 0x00, 0x00, 0x00, 0x00,
        0x48, 0x85, 0xC9,
        0x74, 0x00
    };
    static const char mask[] = "xxx????xxx?";

    PUCHAR p = pattern_scan(base, size, pat, mask, sizeof(pat));
    if (p) {
        PVOID target = resolve_rip_rel(p);
        if ((PUCHAR)target >= base && (PUCHAR)target < base + size)
            return target;
    }
    return NULL;
}

// ==================================================================
// Scrub
// ==================================================================

static BOOLEAN is_our_driver_name(UNICODE_STRING* us)
{
    if (us->Length == 0 || us->Buffer == NULL) return FALSE;
    if (us->Length > 512) return FALSE;
    static const WCHAR needle[] = L"wdisvcmon";
    __try {
        if (us->MaximumLength < us->Length) return FALSE;
        UNICODE_STRING hay = *us;
        for (USHORT i = 0;
             i + (sizeof(needle)/sizeof(WCHAR) - 1) <= hay.Length / sizeof(WCHAR);
             i++) {
            BOOLEAN match = TRUE;
            for (USHORT j = 0; j < sizeof(needle)/sizeof(WCHAR) - 1; j++) {
                WCHAR c = hay.Buffer[i + j];
                if (c >= L'A' && c <= L'Z') c += 32;
                if (c != needle[j]) { match = FALSE; break; }
            }
            if (match) return TRUE;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return FALSE;
}

static VOID scrub_unicode_in_range(PUCHAR base, ULONG size)
{
    __try {
        for (ULONG i = 0; i + 16 <= size; i += 8) {
            UNICODE_STRING* us = (UNICODE_STRING*)(base + i);
            USHORT len = us->Length;
            USHORT max = us->MaximumLength;
            if (len == 0 || len > 512) continue;
            if (max < len || max > 1024) continue;
            if (us->Buffer == NULL) continue;
            if ((ULONG64)us->Buffer < 0xFFFF800000000000ULL) continue;
            if (!MmIsAddressValid(us->Buffer)) continue;
            if (is_our_driver_name(us)) {
                us->Length = 0;
                us->MaximumLength = 0;
                us->Buffer = NULL;
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static VOID scrub_once(VOID)
{
    if (g_PiDdbCacheTable)
        scrub_unicode_in_range((PUCHAR)g_PiDdbCacheTable, 0x1000);
    if (g_KernelHashBucketList)
        scrub_unicode_in_range((PUCHAR)g_KernelHashBucketList, 0x1000);
}

// ==================================================================
// Worker threads
// ==================================================================

static VOID scrub_thread(PVOID ctx)
{
    UNREFERENCED_PARAMETER(ctx);
    LARGE_INTEGER interval;
    interval.QuadPart = -15LL * 1000 * 1000 * 10;
    while (g_Running) {
        KeDelayExecutionThread(KernelMode, FALSE, &interval);
        if (!g_Running) break;
        scrub_once();
    }
    PsTerminateSystemThread(STATUS_SUCCESS);
}

static VOID autowrite_thread(PVOID ctx)
{
    UNREFERENCED_PARAMETER(ctx);
    LARGE_INTEGER tick;
    tick.QuadPart = -1000LL * 10;
    while (g_Running) {
        KeDelayExecutionThread(KernelMode, FALSE, &tick);
        if (!g_Running) break;
        KIRQL old;
        KeAcquireSpinLock(&g_AutowriteLock, &old);
        for (ULONG i = 0; i < AUTOWRITE_SLOTS_MAX; i++) {
            AUTOWRITE_SLOT* s = &g_AutowriteSlots[i];
            if (!s->Active) continue;
            AUTOWRITE_SLOT local = *s;
            KeReleaseSpinLock(&g_AutowriteLock, old);
            do_write(local.Pid, local.Target, local.Payload, local.Size);
            KeAcquireSpinLock(&g_AutowriteLock, &old);
        }
        KeReleaseSpinLock(&g_AutowriteLock, old);
    }
    PsTerminateSystemThread(STATUS_SUCCESS);
}

// ==================================================================
// IOCTL dispatch
// ==================================================================

static NTSTATUS dispatch(PDEVICE_OBJECT dev, PIRP irp)
{
    UNREFERENCED_PARAMETER(dev);
    PIO_STACK_LOCATION sp = IoGetCurrentIrpStackLocation(irp);
    ULONG code  = sp->Parameters.DeviceIoControl.IoControlCode;
    PVOID buf   = irp->AssociatedIrp.SystemBuffer;
    ULONG inLen = sp->Parameters.DeviceIoControl.InputBufferLength;
    ULONG outLen= sp->Parameters.DeviceIoControl.OutputBufferLength;
    NTSTATUS st = STATUS_INVALID_DEVICE_REQUEST;

    switch (code) {

    case PK_IOCTL_READ: {
        if (inLen < sizeof(KR_RW_REQ) || outLen < ((KR_RW_REQ*)buf)->Size) {
            st = STATUS_BUFFER_TOO_SMALL; break;
        }
        KR_RW_REQ* r = (KR_RW_REQ*)buf;
        st = do_read(r->Pid, r->Address, buf, r->Size);
        if (NT_SUCCESS(st)) irp->IoStatus.Information = r->Size;
        break;
    }

    case PK_IOCTL_WRITE: {
        if (inLen < sizeof(KR_RW_REQ)) { st = STATUS_BUFFER_TOO_SMALL; break; }
        KR_RW_REQ* r = (KR_RW_REQ*)buf;
        if (inLen < sizeof(KR_RW_REQ) + r->Size) { st = STATUS_BUFFER_TOO_SMALL; break; }
        PUCHAR data = (PUCHAR)buf + sizeof(KR_RW_REQ);
        st = do_write(r->Pid, r->Address, data, r->Size);
        break;
    }

    case PK_IOCTL_BASE: {
        if (inLen < sizeof(KR_BASE_REQ) || outLen < sizeof(KR_BASE_REQ)) {
            st = STATUS_BUFFER_TOO_SMALL; break;
        }
        KR_BASE_REQ* r = (KR_BASE_REQ*)buf;
        PEPROCESS proc = lookup_process(r->Pid);
        if (!proc) { st = STATUS_NOT_FOUND; break; }
        r->BaseOut = (ULONG64)(ULONG_PTR)PsGetProcessSectionBaseAddress(proc);
        ObDereferenceObject(proc);
        st = STATUS_SUCCESS;
        irp->IoStatus.Information = sizeof(KR_BASE_REQ);
        break;
    }

    case PK_IOCTL_KWRITE_START: {
        if (inLen < sizeof(KR_KWRITE_START_REQ)) { st = STATUS_BUFFER_TOO_SMALL; break; }
        KR_KWRITE_START_REQ* r = (KR_KWRITE_START_REQ*)buf;
        if (r->Size == 0 || r->Size > KWRITE_PAYLOAD_MAX) { st = STATUS_INVALID_PARAMETER; break; }
        KIRQL old; KeAcquireSpinLock(&g_KwriteLock, &old);
        ULONG32 slot = 0xFFFFFFFF;
        for (ULONG32 i = 0; i < KWRITE_SLOTS_MAX; i++) {
            if (!g_KwriteSlots[i].Active) {
                g_KwriteSlots[i].Active = TRUE;
                g_KwriteSlots[i].Pid = r->Pid;
                g_KwriteSlots[i].Target = r->Target;
                g_KwriteSlots[i].Size = r->Size;
                RtlCopyMemory(g_KwriteSlots[i].Payload, r->Payload, r->Size);
                slot = i;
                break;
            }
        }
        KeReleaseSpinLock(&g_KwriteLock, old);
        r->SlotOut = slot;
        st = (slot == 0xFFFFFFFF) ? STATUS_INSUFFICIENT_RESOURCES : STATUS_SUCCESS;
        irp->IoStatus.Information = sizeof(KR_KWRITE_START_REQ);
        break;
    }

    case PK_IOCTL_KWRITE_UPDATE: {
        if (inLen < sizeof(KR_KWRITE_UPDATE_REQ)) { st = STATUS_BUFFER_TOO_SMALL; break; }
        KR_KWRITE_UPDATE_REQ* r = (KR_KWRITE_UPDATE_REQ*)buf;
        if (r->SlotId >= KWRITE_SLOTS_MAX || !g_KwriteSlots[r->SlotId].Active) {
            st = STATUS_INVALID_HANDLE; break;
        }
        if (r->Size > KWRITE_PAYLOAD_MAX) { st = STATUS_INVALID_PARAMETER; break; }
        KIRQL old; KeAcquireSpinLock(&g_KwriteLock, &old);
        RtlCopyMemory(g_KwriteSlots[r->SlotId].Payload, r->Payload, r->Size);
        g_KwriteSlots[r->SlotId].Size = r->Size;
        KeReleaseSpinLock(&g_KwriteLock, old);
        st = STATUS_SUCCESS;
        break;
    }

    case PK_IOCTL_KWRITE_STOP: {
        if (inLen < sizeof(KR_KWRITE_STOP_REQ)) { st = STATUS_BUFFER_TOO_SMALL; break; }
        KR_KWRITE_STOP_REQ* r = (KR_KWRITE_STOP_REQ*)buf;
        if (r->SlotId < KWRITE_SLOTS_MAX)
            g_KwriteSlots[r->SlotId].Active = FALSE;
        st = STATUS_SUCCESS;
        break;
    }

    case PK_IOCTL_ALLOC_RWX: {
        if (inLen < sizeof(KR_ALLOC_REQ) || outLen < sizeof(KR_ALLOC_REQ)) {
            st = STATUS_BUFFER_TOO_SMALL; break;
        }
        KR_ALLOC_REQ* r = (KR_ALLOC_REQ*)buf;
        PEPROCESS proc = lookup_process(r->Pid);
        if (!proc) { st = STATUS_NOT_FOUND; break; }
        KAPC_STATE apc;
        KeStackAttachProcess(proc, &apc);
        PVOID base = NULL; SIZE_T sz = r->Size;
        st = ZwAllocateVirtualMemory(ZwCurrentProcess(), &base, 0, &sz,
                                     MEM_COMMIT | MEM_RESERVE,
                                     PAGE_EXECUTE_READWRITE);
        KeUnstackDetachProcess(&apc);
        ObDereferenceObject(proc);
        if (NT_SUCCESS(st)) {
            r->BaseOut = (ULONG64)(ULONG_PTR)base;
            irp->IoStatus.Information = sizeof(KR_ALLOC_REQ);
        }
        break;
    }

    case PK_IOCTL_PATCH_CODE: {
        if (inLen < sizeof(KR_PATCH_CODE_REQ) || outLen < sizeof(KR_PATCH_CODE_REQ)) {
            st = STATUS_BUFFER_TOO_SMALL; break;
        }
        KR_PATCH_CODE_REQ* r = (KR_PATCH_CODE_REQ*)buf;
        if (r->Size == 0 || r->Size > 32) { st = STATUS_INVALID_PARAMETER; break; }
        st = do_read(r->Pid, r->Addr, r->Stolen, r->Size);
        if (!NT_SUCCESS(st)) break;
        st = do_write(r->Pid, r->Addr, r->Patch, r->Size);
        if (NT_SUCCESS(st)) irp->IoStatus.Information = sizeof(KR_PATCH_CODE_REQ);
        break;
    }

    case PK_IOCTL_AUTOWRITE_START: {
        if (inLen < sizeof(KR_AUTOWRITE_START_REQ)) { st = STATUS_BUFFER_TOO_SMALL; break; }
        KR_AUTOWRITE_START_REQ* r = (KR_AUTOWRITE_START_REQ*)buf;
        if (r->Size == 0 || r->Size > KWRITE_PAYLOAD_MAX) { st = STATUS_INVALID_PARAMETER; break; }
        if (r->IntervalUs < 100 || r->IntervalUs > 1000000) { st = STATUS_INVALID_PARAMETER; break; }
        if (inLen < sizeof(KR_AUTOWRITE_START_REQ) + r->Size) { st = STATUS_BUFFER_TOO_SMALL; break; }
        PUCHAR payload = (PUCHAR)buf + sizeof(KR_AUTOWRITE_START_REQ);
        KIRQL old; KeAcquireSpinLock(&g_AutowriteLock, &old);
        BOOLEAN any = FALSE;
        for (ULONG32 i = 0; i < AUTOWRITE_SLOTS_MAX; i++) {
            if (!g_AutowriteSlots[i].Active) {
                g_AutowriteSlots[i].Active = TRUE;
                g_AutowriteSlots[i].Pid = r->Pid;
                g_AutowriteSlots[i].Target = r->Target;
                g_AutowriteSlots[i].Size = r->Size;
                g_AutowriteSlots[i].IntervalUs = r->IntervalUs;
                RtlCopyMemory(g_AutowriteSlots[i].Payload, payload, r->Size);
                any = TRUE;
                break;
            }
        }
        KeReleaseSpinLock(&g_AutowriteLock, old);
        st = any ? STATUS_SUCCESS : STATUS_INSUFFICIENT_RESOURCES;
        break;
    }

    case PK_IOCTL_AUTOWRITE_SET: {
        if (inLen == 0 || inLen > KWRITE_PAYLOAD_MAX) { st = STATUS_INVALID_PARAMETER; break; }
        KIRQL old; KeAcquireSpinLock(&g_AutowriteLock, &old);
        BOOLEAN any = FALSE;
        for (ULONG32 i = 0; i < AUTOWRITE_SLOTS_MAX; i++) {
            if (g_AutowriteSlots[i].Active && g_AutowriteSlots[i].Size == inLen) {
                RtlCopyMemory(g_AutowriteSlots[i].Payload, buf, inLen);
                any = TRUE;
            }
        }
        KeReleaseSpinLock(&g_AutowriteLock, old);
        st = any ? STATUS_SUCCESS : STATUS_DEVICE_NOT_READY;
        break;
    }

    case PK_IOCTL_AUTOWRITE_STOP: {
        KIRQL old; KeAcquireSpinLock(&g_AutowriteLock, &old);
        for (ULONG32 i = 0; i < AUTOWRITE_SLOTS_MAX; i++)
            g_AutowriteSlots[i].Active = FALSE;
        KeReleaseSpinLock(&g_AutowriteLock, old);
        st = STATUS_SUCCESS;
        break;
    }

    case PK_IOCTL_FIND_PID: {
        if (inLen < sizeof(KR_FIND_PID_REQ) || outLen < sizeof(KR_FIND_PID_REQ)) {
            st = STATUS_BUFFER_TOO_SMALL; break;
        }
        KR_FIND_PID_REQ* r = (KR_FIND_PID_REQ*)buf;
        r->Name[63] = L'\0';
        st = find_pid_by_name(r->Name, &r->PidOut);
        if (NT_SUCCESS(st)) irp->IoStatus.Information = sizeof(KR_FIND_PID_REQ);
        break;
    }

    case PK_IOCTL_FIND_MODULE:
        st = STATUS_NOT_IMPLEMENTED;
        break;

    case PK_IOCTL_HOOK_START:
    case PK_IOCTL_HOOK_STOP:
        st = STATUS_SUCCESS;
        break;

    default:
        break;
    }

    irp->IoStatus.Status = st;
    IofCompleteRequest(irp, IO_NO_INCREMENT);
    return st;
}

// ==================================================================
// Load / unload
// ==================================================================

static VOID driver_unload(PDRIVER_OBJECT drv)
{
    g_Running = FALSE;
    LARGE_INTEGER w; w.QuadPart = -50LL * 1000 * 10;
    KeDelayExecutionThread(KernelMode, FALSE, &w);

    UNICODE_STRING sym; RtlInitUnicodeString(&sym, SYM_NAME);
    IoDeleteSymbolicLink(&sym);
    if (g_Device) IoDeleteDevice(g_Device);
    UNREFERENCED_PARAMETER(drv);
}

NTSTATUS DriverEntry(PDRIVER_OBJECT drv, PUNICODE_STRING reg)
{
    UNREFERENCED_PARAMETER(reg);

    AuxKlibInitialize();

    RtlZeroMemory(g_KwriteSlots, sizeof(g_KwriteSlots));
    RtlZeroMemory(g_AutowriteSlots, sizeof(g_AutowriteSlots));
    KeInitializeSpinLock(&g_KwriteLock);
    KeInitializeSpinLock(&g_AutowriteLock);

    UNICODE_STRING devName, symName;
    RtlInitUnicodeString(&devName, DEV_NAME);
    RtlInitUnicodeString(&symName, SYM_NAME);

    NTSTATUS st = IoCreateDevice(drv, 0, &devName, FILE_DEVICE_UNKNOWN,
                                 0, FALSE, &g_Device);
    if (!NT_SUCCESS(st)) return st;

    st = IoCreateSymbolicLink(&symName, &devName);
    if (!NT_SUCCESS(st)) { IoDeleteDevice(g_Device); return st; }

    for (ULONG i = 0; i <= IRP_MJ_MAXIMUM_FUNCTION; i++)
        drv->MajorFunction[i] = dispatch;
    drv->MajorFunction[IRP_MJ_DEVICE_CONTROL] = dispatch;
    drv->DriverUnload = driver_unload;

    PVOID ntBase = NULL, ciBase = NULL;
    ULONG ntSize = 0, ciSize = 0;
    if (NT_SUCCESS(get_module_info(&ntBase, &ntSize, &ciBase, &ciSize))) {
        g_NtoskrnlBase = ntBase; g_NtoskrnlSize = ntSize;

        if (ntBase && ntSize)
            g_PiDdbCacheTable = locate_piddb_cache((PUCHAR)ntBase, ntSize);

        if (ciBase && ciSize)
            g_KernelHashBucketList = locate_hash_bucket_list((PUCHAR)ciBase, ciSize);

        DbgPrintEx(DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL,
                   "[wk] ntBase=%p piDdb=%p ciBase=%p hashList=%p\n",
                   ntBase, g_PiDdbCacheTable, ciBase, g_KernelHashBucketList);
    }

    HANDLE h;
    if (NT_SUCCESS(PsCreateSystemThread(&h, THREAD_ALL_ACCESS, NULL, NULL, NULL,
                                         scrub_thread, NULL))) {
        ObReferenceObjectByHandle(h, THREAD_ALL_ACCESS, NULL, KernelMode,
                                  (PVOID*)&g_ScrubThread, NULL);
        ZwClose(h);
    }

    if (NT_SUCCESS(PsCreateSystemThread(&h, THREAD_ALL_ACCESS, NULL, NULL, NULL,
                                         autowrite_thread, NULL))) {
        ObReferenceObjectByHandle(h, THREAD_ALL_ACCESS, NULL, KernelMode,
                                  (PVOID*)&g_AutowriteThread, NULL);
        ZwClose(h);
    }

    scrub_once();

    return STATUS_SUCCESS;
}
