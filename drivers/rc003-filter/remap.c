#include "remap.h"

typedef struct _RC003_USAGE_REMAP {
    unsigned short source;
    unsigned short target;
} RC003_USAGE_REMAP;

/* Keyboard-page usages F13..F20. The filter keeps the report shape intact. */
static const RC003_USAGE_REMAP g_usage_remaps[] = {
    {0x0080u, 0x0068u}, /* volume+ -> F13 */
    {0x0081u, 0x0069u}, /* volume- -> F14 */
    {0x00F1u, 0x006Au}, /* back    -> F15 */
    {0x004Au, 0x006Bu}, /* home    -> F16 */
    {0x0065u, 0x006Cu}, /* menu    -> F17 */
    {0x0035u, 0x006Du}, /* TV      -> F18 */
    {0x0066u, 0x006Eu}, /* power   -> F19 */
    {0x003Eu, 0x006Fu}, /* mic     -> F20 */
};

static unsigned short Rc003ReadLe16(const UCHAR *bytes)
{
    return (unsigned short)((unsigned short)bytes[0] |
                            ((unsigned short)bytes[1] << 8));
}

static void Rc003WriteLe16(UCHAR *bytes, unsigned short value)
{
    bytes[0] = (UCHAR)(value & 0xFFu);
    bytes[1] = (UCHAR)((value >> 8) & 0xFFu);
}

static unsigned short Rc003MappedUsage(unsigned short usage)
{
    unsigned long index;
    for (index = 0; index < (sizeof(g_usage_remaps) / sizeof(g_usage_remaps[0])); ++index) {
        if (g_usage_remaps[index].source == usage) {
            return g_usage_remaps[index].target;
        }
    }
    return usage;
}

BOOLEAN Rc003RemapReport(UCHAR *report, SIZE_T length)
{
    unsigned long slot;
    BOOLEAN changed = FALSE;

    if (report == NULL || length < RC003_MIN_REPORT_LENGTH) {
        return FALSE;
    }
    if (report[0] != RC003_REPORT_ID || report[1] != 0x00u || report[2] != 0x00u) {
        return FALSE;
    }

    for (slot = 0; slot < RC003_USAGE_SLOT_COUNT; ++slot) {
        SIZE_T offset = RC003_USAGE_OFFSET + (slot * RC003_USAGE_SLOT_SIZE);
        unsigned short original = Rc003ReadLe16(&report[offset]);
        unsigned short mapped = Rc003MappedUsage(original);
        if (mapped != original) {
            Rc003WriteLe16(&report[offset], mapped);
            changed = TRUE;
        }
    }
    return changed;
}
