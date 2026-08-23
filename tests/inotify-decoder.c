/* SPDX-License-Identifier: GPL-3.0-or-later */

#define main popup_program_main
#include "../pam-u2f-touch-popup.c"
#undef main

#include <assert.h>

static size_t put_event(
    unsigned char *buf,
    size_t offset,
    int wd,
    uint32_t mask,
    const unsigned char *name,
    uint32_t name_len) {
    struct inotify_event event = {
        .wd = wd,
        .mask = mask,
        .cookie = 0,
        .len = name_len,
    };

    memcpy(buf + offset, &event, sizeof(event));
    if (name_len > 0) {
        memcpy(buf + offset + sizeof(event), name, name_len);
    }
    return offset + sizeof(event) + name_len;
}

static void test_valid_unaligned_named_event(void) {
    unsigned char storage[1 + sizeof(struct inotify_event) + 4] = {0};
    unsigned char *buf = storage + 1;
    struct inotify_event event;
    const char *name = NULL;
    size_t offset = 0;
    size_t len = put_event(buf, 0, 7, IN_OPEN, (const unsigned char *)"key\0", 4);

    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_EVENT);
    assert(event.wd == 7);
    assert(event.mask == IN_OPEN);
    assert(name != NULL && strcmp(name, "key") == 0);
    assert(offset == len);
    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_DONE);
}

static void test_valid_unnamed_event(void) {
    unsigned char buf[sizeof(struct inotify_event)] = {0};
    struct inotify_event event;
    const char *name = (const char *)buf;
    size_t offset = 0;
    size_t len = put_event(buf, 0, -1, IN_Q_OVERFLOW, NULL, 0);

    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_EVENT);
    assert(event.mask == IN_Q_OVERFLOW);
    assert(name == NULL);
    assert(offset == len);
}

static void test_truncated_header(void) {
    unsigned char buf[sizeof(struct inotify_event)] = {0};

    for (size_t len = 1; len < sizeof(struct inotify_event); len++) {
        struct inotify_event event;
        const char *name = NULL;
        size_t offset = 0;

        assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_MALFORMED);
    }
}

static void test_oversized_name(void) {
    unsigned char buf[sizeof(struct inotify_event)] = {0};
    struct inotify_event input = {.wd = 1, .mask = IN_OPEN, .cookie = 0, .len = 4};
    struct inotify_event event;
    const char *name = NULL;
    size_t offset = 0;

    memcpy(buf, &input, sizeof(input));
    assert(decode_inotify_event(buf, sizeof(buf), &offset, &event, &name) == DECODE_MALFORMED);
}

static void test_unterminated_name(void) {
    unsigned char buf[sizeof(struct inotify_event) + 4] = {0};
    struct inotify_event event;
    const char *name = NULL;
    size_t offset = 0;
    size_t len = put_event(buf, 0, 1, IN_OPEN, (const unsigned char *)"name", 4);

    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_MALFORMED);
}

static void test_trailing_partial_header(void) {
    unsigned char buf[(2 * sizeof(struct inotify_event)) + 1] = {0};
    struct inotify_event event;
    const char *name = NULL;
    size_t offset = 0;
    size_t len = put_event(buf, 0, 1, IN_OPEN, NULL, 0) + 1;

    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_EVENT);
    assert(decode_inotify_event(buf, len, &offset, &event, &name) == DECODE_MALFORMED);
}

int main(void) {
    test_valid_unaligned_named_event();
    test_valid_unnamed_event();
    test_truncated_header();
    test_oversized_name();
    test_unterminated_name();
    test_trailing_partial_header();
    return 0;
}
