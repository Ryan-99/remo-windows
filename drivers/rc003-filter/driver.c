/* Experimental, device-scoped KMDF read filter. No global keyboard hooks. */
#include <ntddk.h>
#include <wdf.h>
#include "remap.h"

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD Rc003DeviceAdd;
EVT_WDF_IO_QUEUE_IO_READ Rc003Read;
EVT_WDF_REQUEST_COMPLETION_ROUTINE Rc003ReadComplete;

static const WCHAR Rc003HardwareId[] =
    L"HID\\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&012717_PID&32b8_REV&00a4";

/* A second guard in addition to the INF: never activate on another keyboard. */
static BOOLEAN Rc003MatchesDevice(PWDFDEVICE_INIT deviceInit)
{
    WDFMEMORY memory;
    NTSTATUS status;
    const WCHAR *ids;
    SIZE_T bytes, count, begin, end;
    BOOLEAN matches = FALSE;
    UNICODE_STRING wanted;

    status = WdfFdoInitAllocAndQueryProperty(deviceInit, DevicePropertyHardwareID,
        PagedPool, WDF_NO_OBJECT_ATTRIBUTES, &memory);
    if (!NT_SUCCESS(status)) return FALSE;
    ids = (const WCHAR *)WdfMemoryGetBuffer(memory, &bytes);
    count = bytes / sizeof(WCHAR);
    RtlInitUnicodeString(&wanted, Rc003HardwareId);
    for (begin = 0; begin < count && ids[begin] != L'\0'; begin = end + 1) {
        UNICODE_STRING candidate;
        for (end = begin; end < count && ids[end] != L'\0'; ++end) { }
        if (end == count || (end - begin) > MAXUSHORT / sizeof(WCHAR)) break;
        candidate.Buffer = (PWCH)&ids[begin];
        candidate.Length = (USHORT)((end - begin) * sizeof(WCHAR));
        candidate.MaximumLength = candidate.Length;
        if (RtlEqualUnicodeString(&candidate, &wanted, TRUE)) {
            matches = TRUE;
            break;
        }
    }
    WdfObjectDelete(memory);
    return matches;
}

_Use_decl_annotations_
NTSTATUS DriverEntry(PDRIVER_OBJECT driverObject, PUNICODE_STRING registryPath)
{
    WDF_DRIVER_CONFIG config;
    WDF_DRIVER_CONFIG_INIT(&config, Rc003DeviceAdd);
    config.DriverPoolTag = '3cmR';
    return WdfDriverCreate(driverObject, registryPath, WDF_NO_OBJECT_ATTRIBUTES,
        &config, WDF_NO_HANDLE);
}

_Use_decl_annotations_
NTSTATUS Rc003DeviceAdd(WDFDRIVER driver, PWDFDEVICE_INIT deviceInit)
{
    WDFDEVICE device;
    WDF_IO_QUEUE_CONFIG queue;
    NTSTATUS status;
    BOOLEAN matches;
    UNREFERENCED_PARAMETER(driver);

    matches = Rc003MatchesDevice(deviceInit);
    WdfFdoInitSetFilter(deviceInit);
    status = WdfDeviceCreate(&deviceInit, WDF_NO_OBJECT_ATTRIBUTES, &device);
    if (!NT_SUCCESS(status)) return status;
    /* Unmatched/query-failed devices retain WDF's automatic passthrough. */
    if (!matches) return STATUS_SUCCESS;

    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queue, WdfIoQueueDispatchParallel);
    queue.PowerManaged = WdfFalse;
    queue.EvtIoRead = Rc003Read;
    return WdfIoQueueCreate(device, &queue, WDF_NO_OBJECT_ATTRIBUTES, WDF_NO_HANDLE);
}

_Use_decl_annotations_
VOID Rc003Read(WDFQUEUE queue, WDFREQUEST request, size_t length)
{
    WDFIOTARGET target = WdfDeviceGetIoTarget(WdfIoQueueGetDevice(queue));
    UNREFERENCED_PARAMETER(length);
    WdfRequestFormatRequestUsingCurrentType(request);
    WdfRequestSetCompletionRoutine(request, Rc003ReadComplete, WDF_NO_CONTEXT);
    /* Completion owns a sent request, including inline completion. */
    if (!WdfRequestSend(request, target, WDF_NO_SEND_OPTIONS)) {
        WdfRequestComplete(request, WdfRequestGetStatus(request));
    }
}

_Use_decl_annotations_
VOID Rc003ReadComplete(WDFREQUEST request, WDFIOTARGET target,
    PWDF_REQUEST_COMPLETION_PARAMS completion, WDFCONTEXT context)
{
    PVOID buffer;
    SIZE_T capacity;
    ULONG_PTR transferred = completion->IoStatus.Information;
    NTSTATUS status = completion->IoStatus.Status;
    UNREFERENCED_PARAMETER(target);
    UNREFERENCED_PARAMETER(context);

    /* Failed/cancelled reads never expose a report to the remapper. */
    if (NT_SUCCESS(status) && transferred >= RC003_MIN_REPORT_LENGTH &&
        NT_SUCCESS(WdfRequestRetrieveOutputBuffer(request, RC003_MIN_REPORT_LENGTH,
            &buffer, &capacity)) && transferred <= capacity) {
        Rc003RemapReport((UCHAR *)buffer, (SIZE_T)transferred);
    }
    /* Preserve the lower driver's status and actual transfer length exactly. */
    WdfRequestCompleteWithInformation(request, status, transferred);
}
