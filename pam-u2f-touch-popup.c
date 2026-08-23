/* SPDX-License-Identifier: GPL-3.0-or-later */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PAM_U2F_ZENITY_PATH
#define PAM_U2F_ZENITY_PATH "/usr/bin/zenity"
#endif

static volatile sig_atomic_t running = 1;
static volatile sig_atomic_t signal_write_fd = -1;
static pid_t popup_pid = -1;
static int signal_pipe[2] = {-1, -1};

static void wake_event_loop(void) {
    int saved_errno = errno;
    unsigned char byte = 0;

    if (signal_write_fd >= 0) {
        ssize_t ignored = write((int)signal_write_fd, &byte, sizeof(byte));

        (void)ignored;
    }
    errno = saved_errno;
}

static void on_signal(int signum) {
    (void)signum;
    running = 0;
    wake_event_loop();
}

static void on_sigchld(int signum) {
    (void)signum;
    wake_event_loop();
}

static void close_signal_pipe(void) {
    if (signal_pipe[1] >= 0) {
        close(signal_pipe[1]);
        signal_pipe[1] = -1;
    }
    if (signal_pipe[0] >= 0) {
        close(signal_pipe[0]);
        signal_pipe[0] = -1;
    }
}

static int init_signal_pipe(void) {
    if (pipe(signal_pipe) != 0) {
        return -1;
    }

    for (size_t i = 0; i < 2; i++) {
        int status_flags = fcntl(signal_pipe[i], F_GETFL);
        int descriptor_flags = fcntl(signal_pipe[i], F_GETFD);

        if (status_flags < 0 || descriptor_flags < 0 ||
            fcntl(signal_pipe[i], F_SETFL, status_flags | O_NONBLOCK) != 0 ||
            fcntl(signal_pipe[i], F_SETFD, descriptor_flags | FD_CLOEXEC) != 0) {
            close_signal_pipe();
            return -1;
        }
    }
    if (signal_pipe[1] > SIG_ATOMIC_MAX) {
        errno = EMFILE;
        close_signal_pipe();
        return -1;
    }
    signal_write_fd = (sig_atomic_t)signal_pipe[1];
    return 0;
}

static int drain_signal_pipe(void) {
    unsigned char buf[64];

    for (;;) {
        ssize_t len = read(signal_pipe[0], buf, sizeof(buf));

        if (len > 0) {
            continue;
        }
        if (len < 0 && errno == EINTR) {
            continue;
        }
        if (len < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return 0;
        }
        return -1;
    }
}

static const char *env_default(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return (value && value[0]) ? value : fallback;
}

static int env_delay_ms(void) {
    const char *value = getenv("PAM_U2F_TOUCH_DELAY_MS");
    char *end = NULL;
    long parsed;

    if (!value || !value[0]) {
        return 250;
    }

    errno = 0;
    parsed = strtol(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed < 0 || parsed > 5000) {
        return 250;
    }
    return (int)parsed;
}

static long long monotonic_ms(void) {
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0;
    }
    return ((long long)ts.tv_sec * 1000LL) + ((long long)ts.tv_nsec / 1000000LL);
}

static int timeout_until(long long deadline_ms) {
    long long now = monotonic_ms();
    long long remaining = deadline_ms - now;

    if (remaining <= 0) {
        return 0;
    }
    if (remaining > INT_MAX) {
        return INT_MAX;
    }
    return (int)remaining;
}

static bool have_display(void) {
    const char *wayland = getenv("WAYLAND_DISPLAY");
    const char *display = getenv("DISPLAY");
    return (wayland && wayland[0]) || (display && display[0]);
}

static void fill_handled_signal_set(sigset_t *set) {
    sigemptyset(set);
    sigaddset(set, SIGINT);
    sigaddset(set, SIGTERM);
    sigaddset(set, SIGHUP);
    sigaddset(set, SIGCHLD);
}

static int reset_child_signal_handlers(void) {
    struct sigaction action = {0};

    action.sa_handler = SIG_DFL;
    sigemptyset(&action.sa_mask);
    return sigaction(SIGINT, &action, NULL) ||
           sigaction(SIGTERM, &action, NULL) ||
           sigaction(SIGHUP, &action, NULL) ||
           sigaction(SIGCHLD, &action, NULL);
}

static void reap_popup(void) {
    int status;
    pid_t rc;

    if (popup_pid <= 0) {
        return;
    }
    rc = waitpid(popup_pid, &status, WNOHANG);
    if (rc == popup_pid) {
        if (WIFEXITED(status) && WEXITSTATUS(status) == 127) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: unable to execute Zenity\n");
        }
        popup_pid = -1;
    } else if (rc < 0 && errno == ECHILD) {
        popup_pid = -1;
    }
}

static void close_popup(void) {
    int status;
    pid_t pid;
    pid_t rc;

    reap_popup();
    if (popup_pid <= 0) {
        return;
    }
    pid = popup_pid;
    popup_pid = -1;
    (void)kill(pid, SIGTERM);
    for (int i = 0; i < 10; i++) {
        rc = waitpid(pid, &status, WNOHANG);
        if (rc == pid || (rc < 0 && errno == ECHILD)) {
            return;
        }
        nanosleep(&(const struct timespec){.tv_sec = 0, .tv_nsec = 100000000L}, NULL);
    }
    (void)kill(pid, SIGKILL);
    do {
        rc = waitpid(pid, &status, 0);
    } while (rc < 0 && errno == EINTR);
}

static void show_popup(const char *zenity, const char *title, const char *message) {
    int devnull;
    int fork_errno;
    pid_t child_pid;
    sigset_t handled_signals;
    sigset_t previous_mask;
    reap_popup();
    if (popup_pid > 0 || !have_display()) {
        return;
    }

    fill_handled_signal_set(&handled_signals);
    if (sigprocmask(SIG_BLOCK, &handled_signals, &previous_mask) != 0) {
        perror("sigprocmask");
        return;
    }

    child_pid = fork();
    fork_errno = errno;
    if (child_pid != 0) {
        if (child_pid > 0) {
            popup_pid = child_pid;
        }
        if (sigprocmask(SIG_SETMASK, &previous_mask, NULL) != 0) {
            perror("sigprocmask");
            running = 0;
            wake_event_loop();
            return;
        }
    }
    if (child_pid < 0) {
        errno = fork_errno;
        perror("fork");
        return;
    }
    if (child_pid > 0) {
        return;
    }

    if (reset_child_signal_handlers() != 0) {
        _exit(127);
    }
    close(signal_pipe[1]);
    close(signal_pipe[0]);
    if (sigprocmask(SIG_SETMASK, &previous_mask, NULL) != 0) {
        _exit(127);
    }

    (void)setsid();
    devnull = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (devnull >= 0) {
        (void)dup2(devnull, STDIN_FILENO);
        (void)dup2(devnull, STDOUT_FILENO);
        (void)dup2(devnull, STDERR_FILENO);
        close(devnull);
    }

    execl(
        zenity,
        "zenity",
        "--info",
        "--title",
        title,
        "--text",
        message,
        "--no-markup",
        "--width=480",
        (char *)NULL);
    _exit(127);
}

static int split_parent_base(const char *path, char *parent, size_t parent_size, const char **base) {
    const char *slash = strrchr(path, '/');
    size_t len;

    if (!slash || slash == path) {
        if (snprintf(parent, parent_size, "%s", slash ? "/" : ".") >= (int)parent_size) {
            return -1;
        }
        *base = slash ? slash + 1 : path;
        return 0;
    }

    len = (size_t)(slash - path);
    if (len >= parent_size) {
        return -1;
    }
    memcpy(parent, path, len);
    parent[len] = '\0';
    *base = slash + 1;
    return 0;
}

static void handle_target_event(
    uint32_t mask,
    bool *request_active,
    bool *popup_due,
    long long *popup_deadline_ms,
    int delay_ms,
    const char *zenity,
    const char *title,
    const char *message) {
    if (mask & IN_ISDIR) {
        return;
    }

    if (mask & IN_OPEN) {
        if (!*request_active) {
            *request_active = true;
            if (delay_ms == 0) {
                show_popup(zenity, title, message);
                *popup_due = false;
            } else {
                *popup_due = true;
                *popup_deadline_ms = monotonic_ms() + delay_ms;
            }
        }
    }

    if (mask & IN_CLOSE) {
        *request_active = false;
        *popup_due = false;
        close_popup();
    }

    if (mask & (IN_DELETE | IN_MOVED_FROM)) {
        *request_active = false;
        *popup_due = false;
        close_popup();
    }
}

enum decode_result {
    DECODE_DONE,
    DECODE_EVENT,
    DECODE_MALFORMED,
};

static enum decode_result decode_inotify_event(
    const unsigned char *buf,
    size_t len,
    size_t *offset,
    struct inotify_event *event,
    const char **name) {
    size_t remaining;

    if (*offset == len) {
        return DECODE_DONE;
    }
    if (*offset > len) {
        return DECODE_MALFORMED;
    }

    remaining = len - *offset;
    if (remaining < sizeof(*event)) {
        return DECODE_MALFORMED;
    }
    memcpy(event, buf + *offset, sizeof(*event));
    if ((size_t)event->len > remaining - sizeof(*event)) {
        return DECODE_MALFORMED;
    }

    *name = NULL;
    if (event->len > 0) {
        *name = (const char *)(buf + *offset + sizeof(*event));
        if (memchr(*name, '\0', event->len) == NULL) {
            return DECODE_MALFORMED;
        }
    }

    *offset += sizeof(*event) + (size_t)event->len;
    return DECODE_EVENT;
}

static int process_inotify_buffer(
    const unsigned char *buf,
    size_t len,
    int dir_wd,
    const char *base,
    bool *request_active,
    bool *popup_due,
    long long *popup_deadline_ms,
    int delay_ms,
    const char *zenity,
    const char *title,
    const char *message) {
    size_t offset = 0;

    for (;;) {
        struct inotify_event event;
        const char *name;
        enum decode_result decoded = decode_inotify_event(buf, len, &offset, &event, &name);

        if (decoded == DECODE_DONE) {
            return 0;
        }
        if (decoded == DECODE_MALFORMED) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: malformed inotify event stream\n");
            return -1;
        }

        if (event.mask & IN_Q_OVERFLOW) {
            (void)fprintf(
                stderr,
                "pam-u2f-touch-popup: inotify event queue overflowed; resetting popup state\n");
            *request_active = false;
            *popup_due = false;
            close_popup();
        } else if (event.wd == dir_wd && name && strcmp(name, base) == 0) {
            handle_target_event(
                event.mask,
                request_active,
                popup_due,
                popup_deadline_ms,
                delay_ms,
                zenity,
                title,
                message);
        } else if (event.wd == dir_wd &&
                   (event.mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED | IN_UNMOUNT))) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: watched directory is no longer available\n");
            return -1;
        }
    }
}

static int open_private_parent(const char *parent) {
    struct stat info;
    int fd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);

    if (fd < 0) {
        perror("open authpending parent");
        return -1;
    }
    if (fstat(fd, &info) != 0) {
        perror("fstat authpending parent");
        close(fd);
        return -1;
    }
    if (!S_ISDIR(info.st_mode) || info.st_uid != geteuid() ||
        (info.st_mode & (S_IRWXG | S_IRWXO)) != 0) {
        (void)fprintf(
            stderr,
            "pam-u2f-touch-popup: authpending parent must be an owner-only directory owned by uid %ld: %s\n",
            (long)geteuid(),
            parent);
        close(fd);
        errno = EPERM;
        return -1;
    }
    return fd;
}

static int event_loop(
    const char *parent,
    const char *base,
    const char *zenity,
    const char *title,
    const char *message) {
    char watch_path[64];
    int parent_fd = open_private_parent(parent);
    int fd;
    int dir_wd;
    /* inotify coalesces identical events, so this must not be an open counter. */
    bool request_active = false;
    bool popup_due = false;
    bool failed = false;
    long long popup_deadline_ms = 0;
    int delay_ms = env_delay_ms();
    unsigned char buf[4096];
    struct pollfd poll_fds[2];

    if (parent_fd < 0) {
        return 1;
    }
    if (snprintf(watch_path, sizeof(watch_path), "/proc/self/fd/%d", parent_fd) >=
        (int)sizeof(watch_path)) {
        (void)fprintf(stderr, "pam-u2f-touch-popup: authpending parent descriptor is too long\n");
        close(parent_fd);
        return 1;
    }

    fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (fd < 0) {
        perror("inotify_init1");
        close(parent_fd);
        return 1;
    }

    dir_wd = inotify_add_watch(
        fd,
        watch_path,
        IN_OPEN | IN_CLOSE | IN_DELETE | IN_MOVED_FROM | IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT);
    close(parent_fd);
    if (dir_wd < 0) {
        perror("inotify_add_watch parent");
        close(fd);
        return 1;
    }

    poll_fds[0].fd = fd;
    poll_fds[0].events = POLLIN;
    poll_fds[1].fd = signal_pipe[0];
    poll_fds[1].events = POLLIN;

    while (running) {
        int rc;

        reap_popup();
        if (popup_due && request_active && monotonic_ms() >= popup_deadline_ms) {
            show_popup(zenity, title, message);
            popup_due = false;
        }
        rc = poll(poll_fds, 2, popup_due ? timeout_until(popup_deadline_ms) : -1);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("poll");
            failed = true;
            break;
        }
        if (rc == 0) {
            continue;
        }
        if (poll_fds[1].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: signal pipe poll failed\n");
            failed = true;
            break;
        }
        if ((poll_fds[1].revents & POLLIN) && drain_signal_pipe() != 0) {
            perror("read signal pipe");
            failed = true;
            break;
        }
        reap_popup();
        if (!running) {
            break;
        }
        if (poll_fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: inotify poll failed\n");
            failed = true;
            break;
        }
        if (!(poll_fds[0].revents & POLLIN)) {
            continue;
        }

        for (;;) {
            ssize_t len = read(fd, buf, sizeof(buf));

            if (len < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    break;
                }
                if (errno == EINTR) {
                    continue;
                }
                perror("read inotify");
                failed = true;
                running = 0;
                break;
            }
            if (len == 0) {
                (void)fprintf(stderr, "pam-u2f-touch-popup: unexpected end of inotify stream\n");
                failed = true;
                running = 0;
                break;
            }

            if (process_inotify_buffer(
                    buf,
                    (size_t)len,
                    dir_wd,
                    base,
                    &request_active,
                    &popup_due,
                    &popup_deadline_ms,
                    delay_ms,
                    zenity,
                    title,
                    message) != 0) {
                failed = true;
                running = 0;
                break;
            }
        }
    }

    close_popup();
    (void)inotify_rm_watch(fd, dir_wd);
    close(fd);
    return failed ? 1 : 0;
}

static int install_signal_handlers(void) {
    struct sigaction action = {0};

    action.sa_flags = 0;
    sigemptyset(&action.sa_mask);
    action.sa_handler = on_signal;
    if (sigaction(SIGINT, &action, NULL) != 0 ||
        sigaction(SIGTERM, &action, NULL) != 0 ||
        sigaction(SIGHUP, &action, NULL) != 0) {
        return -1;
    }

    action.sa_handler = on_sigchld;
    if (sigaction(SIGCHLD, &action, NULL) != 0) {
        return -1;
    }
    return 0;
}

int main(void) {
    char default_path[PATH_MAX];
    char parent[PATH_MAX];
    const char *base = NULL;
    const char *path;
    const char *zenity;
    const char *title = env_default("PAM_U2F_TOUCH_TITLE", "Security key touch required");
    const char *message = env_default(
        "PAM_U2F_TOUCH_MESSAGE",
        "Touch your security key to approve authentication.");

#ifdef PAM_U2F_TESTING
    zenity = env_default("PAM_U2F_ZENITY", PAM_U2F_ZENITY_PATH);
#else
    if (getenv("PAM_U2F_ZENITY") != NULL) {
        (void)fprintf(
            stderr,
            "pam-u2f-touch-popup: PAM_U2F_ZENITY is test-only; configure the executable at build time\n");
        return 1;
    }
    zenity = PAM_U2F_ZENITY_PATH;
#endif
    if (zenity[0] != '/') {
        (void)fprintf(stderr, "pam-u2f-touch-popup: Zenity executable must be an absolute path\n");
        return 1;
    }

    if (init_signal_pipe() != 0) {
        perror("pipe");
        return 1;
    }

    if (install_signal_handlers() != 0) {
        perror("sigaction");
        return 1;
    }

    path = getenv("PAM_U2F_AUTHPENDING_FILE");
    if (!path || !path[0]) {
        if (snprintf(default_path, sizeof(default_path), "/var/run/user/%ld/pam-u2f-authpending", (long)getuid()) >=
            (int)sizeof(default_path)) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: authpending path is too long\n");
            return 1;
        }
        path = default_path;
    }

    if (split_parent_base(path, parent, sizeof(parent), &base) != 0 || !base || !base[0]) {
        (void)fprintf(stderr, "pam-u2f-touch-popup: invalid authpending path: %s\n", path);
        return 1;
    }

    return event_loop(parent, base, zenity, title, message);
}
