/* SPDX-License-Identifier: GPL-3.0-or-later */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/inotify.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;
static pid_t popup_pid = -1;

static void on_signal(int signum) {
    (void)signum;
    running = 0;
}

static void on_sigchld(int signum) {
    (void)signum;
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

static void show_popup(const char *title, const char *message) {
    int devnull;
    const char *zenity = env_default("PAM_U2F_ZENITY", "zenity");

    reap_popup();
    if (popup_pid > 0 || !have_display()) {
        return;
    }

    popup_pid = fork();
    if (popup_pid < 0) {
        perror("fork");
        return;
    }
    if (popup_pid > 0) {
        return;
    }

    (void)setsid();
    devnull = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (devnull >= 0) {
        (void)dup2(devnull, STDIN_FILENO);
        (void)dup2(devnull, STDOUT_FILENO);
        (void)dup2(devnull, STDERR_FILENO);
        close(devnull);
    }

    execlp(
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
    const char *title,
    const char *message) {
    if (mask & IN_ISDIR) {
        return;
    }

    if (mask & IN_OPEN) {
        if (!*request_active) {
            *request_active = true;
            if (delay_ms == 0) {
                show_popup(title, message);
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

static int event_loop(const char *parent, const char *base, const char *title, const char *message) {
    int fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    int dir_wd;
    /* inotify coalesces identical events, so this must not be an open counter. */
    bool request_active = false;
    bool popup_due = false;
    bool failed = false;
    long long popup_deadline_ms = 0;
    int delay_ms = env_delay_ms();
    _Alignas(struct inotify_event) char buf[4096];
    struct pollfd pfd;

    if (fd < 0) {
        perror("inotify_init1");
        return 1;
    }

    dir_wd = inotify_add_watch(
        fd,
        parent,
        IN_OPEN | IN_CLOSE | IN_DELETE | IN_MOVED_FROM | IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT);
    if (dir_wd < 0) {
        perror("inotify_add_watch parent");
        close(fd);
        return 1;
    }

    pfd.fd = fd;
    pfd.events = POLLIN;

    while (running) {
        int rc;

        reap_popup();
        if (popup_due && request_active && monotonic_ms() >= popup_deadline_ms) {
            show_popup(title, message);
            popup_due = false;
        }
        rc = poll(&pfd, 1, popup_due ? timeout_until(popup_deadline_ms) : -1);
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
        if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
            (void)fprintf(stderr, "pam-u2f-touch-popup: inotify poll failed\n");
            failed = true;
            break;
        }

        for (;;) {
            ssize_t len = read(fd, buf, sizeof(buf));
            char *ptr = buf;

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

            while (ptr < buf + len) {
                struct inotify_event *event = (struct inotify_event *)ptr;

                if (event->mask & IN_Q_OVERFLOW) {
                    (void)fprintf(
                        stderr,
                        "pam-u2f-touch-popup: inotify event queue overflowed; resetting popup state\n");
                    request_active = false;
                    popup_due = false;
                    close_popup();
                } else if (event->wd == dir_wd && event->len > 0 && strcmp(event->name, base) == 0) {
                    handle_target_event(
                        event->mask,
                        &request_active,
                        &popup_due,
                        &popup_deadline_ms,
                        delay_ms,
                        title,
                        message);
                } else if (event->wd == dir_wd &&
                           (event->mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED | IN_UNMOUNT))) {
                    (void)fprintf(stderr, "pam-u2f-touch-popup: watched directory is no longer available\n");
                    failed = true;
                    running = 0;
                }

                ptr += sizeof(struct inotify_event) + event->len;
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
    const char *title = env_default("PAM_U2F_TOUCH_TITLE", "Security key touch required");
    const char *message = env_default(
        "PAM_U2F_TOUCH_MESSAGE",
        "Touch your security key to approve authentication.");

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

    return event_loop(parent, base, title, message);
}
