#include <stdio.h>
#include <string.h>
#include "../remap.h"

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition); \
        return 1; \
    } \
} while (0)

static unsigned short expected_usage(unsigned int usage)
{
    switch (usage) {
    case 0x80: return 0x68;
    case 0x81: return 0x69;
    case 0xF1: return 0x6A;
    case 0x4A: return 0x6B;
    case 0x65: return 0x6C;
    case 0x35: return 0x6D;
    case 0x66: return 0x6E;
    case 0x3E: return 0x6F;
    default: return (unsigned short)usage;
    }
}

static int exhaustive_usage_test(void)
{
    unsigned int usage;
    size_t slot;
    for (slot = 0; slot < 3; ++slot) {
        for (usage = 0; usage <= 0xFFFFu; ++usage) {
            unsigned char report[11] = {0xA5, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0x5A};
            unsigned char expected[11];
            size_t offset = 1 + 3 + slot * 2;
            unsigned short mapped = expected_usage(usage);
            report[offset] = (unsigned char)usage;
            report[offset + 1] = (unsigned char)(usage >> 8);
            memcpy(expected, report, sizeof(report));
            expected[offset] = (unsigned char)mapped;
            expected[offset + 1] = (unsigned char)(mapped >> 8);
            CHECK(!!Rc003RemapReport(report + 1, 9) == (mapped != usage));
            CHECK(memcmp(report, expected, sizeof(report)) == 0);
            CHECK(!Rc003RemapReport(report + 1, 9));
            CHECK(memcmp(report, expected, sizeof(report)) == 0);
        }
    }
    return 0;
}

static int length_and_prefix_test(void)
{
    size_t length;
    size_t i;
    unsigned int value;
    unsigned char report[123];
    unsigned char expected[123];
    for (length = 0; length <= 121; ++length) {
        memset(report, 0xA5, sizeof(report));
        report[1] = 1;
        report[2] = report[3] = 0;
        report[4] = 0x80;
        report[6] = 0xF1;
        report[8] = 0x3E;
        report[5] = report[7] = report[9] = 0;
        memcpy(expected, report, sizeof(report));
        if (length >= 9) {
            expected[4] = 0x68;
            expected[6] = 0x6A;
            expected[8] = 0x6F;
        }
        CHECK(!!Rc003RemapReport(report + 1, length) == (length >= 9));
        CHECK(memcmp(report, expected, sizeof(report)) == 0);
        CHECK(!Rc003RemapReport(NULL, length));
    }
    for (i = 0; i < 3; ++i) {
        for (value = 0; value <= 255; ++value) {
            unsigned char invalid[9] = {1, 0, 0, 0x80, 0, 0xF1, 0, 0x3E, 0};
            unsigned char before[9];
            if (value == invalid[i]) continue;
            invalid[i] = (unsigned char)value;
            memcpy(before, invalid, sizeof(before));
            CHECK(!Rc003RemapReport(invalid, sizeof(invalid)));
            CHECK(memcmp(invalid, before, sizeof(invalid)) == 0);
        }
    }
    return 0;
}

static int report_sequence_test(void)
{
    static const unsigned char input[][9] = {
        {1, 0, 0, 0x3E, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x3E, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x80, 0, 0x3E, 0, 0xF1, 0},
        {1, 0, 0, 0x3E, 0, 0xF1, 0, 0, 0},
        {1, 0, 0, 0, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x3E, 0, 0, 0, 0, 0},
        {1, 0, 0, 0, 0, 0, 0, 0, 0}
    };
    static const unsigned char expected[][9] = {
        {1, 0, 0, 0x6F, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x6F, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x68, 0, 0x6F, 0, 0x6A, 0},
        {1, 0, 0, 0x6F, 0, 0x6A, 0, 0, 0},
        {1, 0, 0, 0, 0, 0, 0, 0, 0},
        {1, 0, 0, 0x6F, 0, 0, 0, 0, 0},
        {1, 0, 0, 0, 0, 0, 0, 0, 0}
    };
    unsigned char previous[256] = {0};
    unsigned int downs = 0, ups = 0;
    size_t frame;
    for (frame = 0; frame < sizeof(input) / sizeof(input[0]); ++frame) {
        unsigned char report[9];
        unsigned char active[256] = {0};
        size_t i;
        memcpy(report, input[frame], sizeof(report));
        CHECK(!!Rc003RemapReport(report, sizeof(report)) == (frame != 4 && frame != 6));
        CHECK(memcmp(report, expected[frame], sizeof(report)) == 0);
        for (i = 3; i < 9; i += 2) if (report[i]) active[report[i]] = 1;
        for (i = 1; i < 256; ++i) {
            downs += active[i] && !previous[i];
            ups += previous[i] && !active[i];
        }
        memcpy(previous, active, sizeof(previous));
    }
    CHECK(downs == 4 && ups == 4);
    return 0;
}

int main(void)
{
    if (exhaustive_usage_test() || length_and_prefix_test() || report_sequence_test()) return 1;
    puts("PASS: 196608 usage/slot cases, idempotence, lengths 0..121, invalid prefixes, release/repress sequence");
    return 0;
}
