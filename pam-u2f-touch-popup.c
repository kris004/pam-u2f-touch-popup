#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;
static pid_t popup_pid = -1;

static void on_signal(int signum) {
    (void)signum;
    running = 0;
    if (popup_pid > 0) {
        kill(popup_pid, SIGTERM);
    }
}

static const char *env_default(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return (value && value[0]) ? value : fallback;
}

static bool have_display(void) {
    const char *wayland = getenv("WAYLAND_DISPLAY");
    const char *display = getenv("DISPLAY");
    return (wayland && wayland[0]) || (display && display[0]);
}

static void reap_popup(void) {
    int status;

    if (popup_pid <= 0) {
        return;
    }
    if (waitpid(popup_pid, &status, WNOHANG) == popup_pid) {
        popup_pid = -1;
    }
}

static void close_popup(void) {
    int status;

    reap_popup();
    if (popup_pid <= 0) {
        return;
    }
    kill(popup_pid, SIGTERM);
    for (int i = 0; i < 10; i++) {
        if (waitpid(popup_pid, &status, WNOHANG) == popup_pid) {
            popup_pid = -1;
            return;
        }
        nanosleep(&(const struct timespec){.tv_sec = 0, .tv_nsec = 100000000L}, NULL);
    }
    kill(popup_pid, SIGKILL);
    (void)waitpid(popup_pid, &status, 0);
    popup_pid = -1;
}

static void show_popup(const char *title, const char *message) {
    int devnull;
    const char *zenity = env_default("PAM_U2F_ZENITY", "/usr/bin/zenity");

    reap_popup();
    if (popup_pid > 0 || !have_display() || access(zenity, X_OK) != 0) {
        return;
    }

    popup_pid = fork();
    if (popup_pid != 0) {
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

static int watch_file(int fd, const char *path) {
    int mask = IN_OPEN | IN_CLOSE | IN_DELETE_SELF | IN_MOVE_SELF;
    int wd = inotify_add_watch(fd, path, (uint32_t)mask);

    if (wd < 0 && errno != ENOENT) {
        perror("inotify_add_watch file");
    }
    return wd;
}

static void reset_file_watch(int fd, const char *path, int *file_wd) {
    if (*file_wd >= 0) {
        (void)inotify_rm_watch(fd, *file_wd);
        *file_wd = -1;
    }
    *file_wd = watch_file(fd, path);
}

static void handle_file_event(uint32_t mask, int *pending_opens, const char *title, const char *message) {
    if (mask & IN_OPEN) {
        if (*pending_opens == 0) {
            show_popup(title, message);
        }
        (*pending_opens)++;
    }

    if (mask & IN_CLOSE) {
        if (*pending_opens > 0) {
            (*pending_opens)--;
        }
        if (*pending_opens == 0) {
            close_popup();
        }
    }

    if (mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED)) {
        *pending_opens = 0;
        close_popup();
    }
}

static int event_loop(const char *path, const char *parent, const char *base, const char *title, const char *message) {
    int fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    int file_wd = -1;
    int dir_wd;
    int pending_opens = 0;
    char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
    struct pollfd pfd;

    if (fd < 0) {
        perror("inotify_init1");
        return 1;
    }

    dir_wd = inotify_add_watch(fd, parent, IN_CREATE | IN_MOVED_TO);
    if (dir_wd < 0) {
        perror("inotify_add_watch parent");
        close(fd);
        return 1;
    }
    reset_file_watch(fd, path, &file_wd);

    pfd.fd = fd;
    pfd.events = POLLIN;

    while (running) {
        int rc;

        reap_popup();
        rc = poll(&pfd, 1, 1000);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("poll");
            break;
        }
        if (rc == 0) {
            continue;
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
                running = 0;
                break;
            }

            while (ptr < buf + len) {
                struct inotify_event *event = (struct inotify_event *)ptr;

                if (event->wd == dir_wd && event->len > 0 && strcmp(event->name, base) == 0) {
                    reset_file_watch(fd, path, &file_wd);
                } else if (event->wd == file_wd) {
                    handle_file_event(event->mask, &pending_opens, title, message);
                    if (event->mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED)) {
                        file_wd = -1;
                        reset_file_watch(fd, path, &file_wd);
                    }
                }

                ptr += sizeof(struct inotify_event) + event->len;
            }
        }
    }

    close_popup();
    if (file_wd >= 0) {
        (void)inotify_rm_watch(fd, file_wd);
    }
    (void)inotify_rm_watch(fd, dir_wd);
    close(fd);
    return 0;
}

int main(void) {
    char default_path[PATH_MAX];
    char parent[PATH_MAX];
    const char *base = NULL;
    const char *path;
    const char *runtime_dir;
    const char *title = env_default("PAM_U2F_TOUCH_TITLE", "YubiKey touch required");
    const char *message = env_default(
        "PAM_U2F_TOUCH_MESSAGE",
        "Touch your YubiKey Bio to approve sudo/authentication.");

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGHUP, on_signal);

    path = getenv("PAM_U2F_AUTHPENDING_FILE");
    if (!path || !path[0]) {
        runtime_dir = env_default("XDG_RUNTIME_DIR", NULL);
        if (runtime_dir) {
            if (snprintf(default_path, sizeof(default_path), "%s/pam-u2f-authpending", runtime_dir) >= (int)sizeof(default_path)) {
                fprintf(stderr, "pam-u2f-touch-popup: authpending path is too long\n");
                return 1;
            }
        } else {
            if (snprintf(default_path, sizeof(default_path), "/run/user/%ld/pam-u2f-authpending", (long)getuid()) >= (int)sizeof(default_path)) {
                fprintf(stderr, "pam-u2f-touch-popup: authpending path is too long\n");
                return 1;
            }
        }
        path = default_path;
    }

    if (split_parent_base(path, parent, sizeof(parent), &base) != 0 || !base || !base[0]) {
        fprintf(stderr, "pam-u2f-touch-popup: invalid authpending path: %s\n", path);
        return 1;
    }

    return event_loop(path, parent, base, title, message);
}
