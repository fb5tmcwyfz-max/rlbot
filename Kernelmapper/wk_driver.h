// language: C, file: wk_driver.h, target: Windows 11 23H2 (build 22631), WDK
// IOCTL contract — bit-for-bit compatible with
// prism_sdk/low/mem.py + prism_sdk/low/kwrite.py + pk_pymem.py.

#pragma once
#include <ntifs.h>
#include <ntddk.h>

#define DEV_NAME  L"\\Device\\WdiSvcMon"
#define SYM_NAME  L"\\GLOBAL??\\WdiSvcMon"
#define POOL_TAG  'sfTN'   // 'Ntfs' spoof

// ---- IOCTL codes (must match pk_pymem.py / mem.py / kwrite.py) ----
#define PK_IOCTL_READ            0x800
#define PK_IOCTL_WRITE           0x801
#define PK_IOCTL_BASE            0x802
#define PK_IOCTL_KWRITE_START    0x806
#define PK_IOCTL_KWRITE_UPDATE   0x807
#define PK_IOCTL_KWRITE_STOP     0x808
#define PK_IOCTL_ALLOC_RWX       0x809
#define PK_IOCTL_PATCH_CODE      0x80A
#define PK_IOCTL_AUTOWRITE_START 0x810
#define PK_IOCTL_AUTOWRITE_SET   0x811
#define PK_IOCTL_AUTOWRITE_STOP  0x812
#define PK_IOCTL_FIND_MODULE     0x813
#define PK_IOCTL_FIND_PID        0x814
#define PK_IOCTL_HOOK_START      0x820
#define PK_IOCTL_HOOK_STOP       0x821

// ---- struct layouts (see pk_pymem.py for byte-for-byte source of truth) ----
#pragma pack(push, 1)

typedef struct _KR_RW_REQ {          // <QII>  = 16 bytes
    ULONG64 Address;
    ULONG32 Pid;
    ULONG32 Size;
} KR_RW_REQ;

typedef struct _KR_BASE_REQ {        // <IIQ> = 16 bytes
    ULONG32 Pid;
    ULONG32 Pad;
    ULONG64 BaseOut;
} KR_BASE_REQ;

typedef struct _KR_KWRITE_START_REQ { // <QQII64sII> = 96 bytes
    ULONG64 Target;
    ULONG64 Pid;
    ULONG32 Size;
    ULONG32 Pad0;
    UCHAR   Payload[64];
    ULONG32 SlotOut;
    ULONG32 Pad1;
} KR_KWRITE_START_REQ;

typedef struct _KR_KWRITE_UPDATE_REQ { // <II64s> = 72 bytes
    ULONG32 SlotId;
    ULONG32 Size;
    UCHAR   Payload[64];
} KR_KWRITE_UPDATE_REQ;

typedef struct _KR_KWRITE_STOP_REQ {   // <II> = 8 bytes
    ULONG32 SlotId;
    ULONG32 Pad;
} KR_KWRITE_STOP_REQ;

typedef struct _KR_ALLOC_REQ {         // <QIIQ> = 24 bytes
    ULONG64 Pid;
    ULONG32 Size;
    ULONG32 Pad;
    ULONG64 BaseOut;
} KR_ALLOC_REQ;

typedef struct _KR_PATCH_CODE_REQ {    // <QQII32s32s> = 88 bytes
    ULONG64 Pid;
    ULONG64 Addr;
    ULONG32 Size;
    ULONG32 Pad;
    UCHAR   Patch[32];
    UCHAR   Stolen[32];
} KR_PATCH_CODE_REQ;

typedef struct _KR_AUTOWRITE_START_REQ { // <QIIII> + payload = 24+ bytes
    ULONG64 Target;
    ULONG32 Pid;
    ULONG32 Size;
    ULONG32 IntervalUs;
    ULONG32 Pad;
    // followed by Size bytes of initial payload
} KR_AUTOWRITE_START_REQ;

typedef struct _KR_HOOK_REQ {          // <IIQ> = 16 bytes
    ULONG32 Pid;
    ULONG32 Pad;
    ULONG64 CarAddr;
} KR_HOOK_REQ;

typedef struct _KR_FIND_PID_REQ {      // 128-byte UTF-16 name + u32 pid + u32 pad = 136
    WCHAR   Name[64];
    ULONG32 PidOut;
    ULONG32 Pad;
} KR_FIND_PID_REQ;

#pragma pack(pop)

#define KWRITE_SLOTS_MAX    64
#define KWRITE_PAYLOAD_MAX  64
#define AUTOWRITE_SLOTS_MAX 8

typedef struct _KWRITE_SLOT {
    BOOLEAN  Active;
    ULONG64  Pid;
    ULONG64  Target;
    ULONG32  Size;
    UCHAR    Payload[KWRITE_PAYLOAD_MAX];
} KWRITE_SLOT;

typedef struct _AUTOWRITE_SLOT {
    BOOLEAN  Active;
    ULONG64  Pid;
    ULONG64  Target;
    ULONG32  Size;
    ULONG32  IntervalUs;
    UCHAR    Payload[KWRITE_PAYLOAD_MAX];
} AUTOWRITE_SLOT;
