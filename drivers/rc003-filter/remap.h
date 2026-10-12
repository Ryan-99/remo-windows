#pragma once

/*
 * RC003 HID report remapping contract.
 *
 * The filter sees a HID-class buffer that may be larger than the nine-byte
 * Bluetooth payload. Only the report prefix and the first three 16-bit usage
 * slots are touched; all other bytes remain unchanged.
 */

#ifdef RC003_HOST_TEST
#include <stddef.h>
#include <stdint.h>
typedef uint8_t UCHAR;
typedef size_t SIZE_T;
typedef int BOOLEAN;
#ifndef TRUE
#define TRUE 1
#endif
#ifndef FALSE
#define FALSE 0
#endif
#else
#include <ntddk.h>
#endif

#define RC003_REPORT_ID 0x01u
#define RC003_REPORT_PREFIX_LENGTH 3u
#define RC003_USAGE_SLOT_COUNT 3u
#define RC003_USAGE_OFFSET 3u
#define RC003_USAGE_SLOT_SIZE 2u
#define RC003_MIN_REPORT_LENGTH 9u

/* Returns TRUE when at least one usage was translated. */
BOOLEAN Rc003RemapReport(UCHAR *report, SIZE_T length);
