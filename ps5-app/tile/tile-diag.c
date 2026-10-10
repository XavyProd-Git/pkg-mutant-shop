/* dlopen diagnostic: which system modules can a payload load? Reports each to PC:9097. */
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <dlfcn.h>

#ifndef REPORT_HOST
#define REPORT_HOST "10.0.0.76"
#endif
#define REPORT_PORT 9097

static void pc_log(const char *msg) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return;
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(REPORT_PORT);
    a.sin_addr.s_addr = inet_addr(REPORT_HOST);
    if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) {
        char line[400]; int n = snprintf(line, sizeof line, "[diag] %s\n", msg);
        write(s, line, n);
    }
    close(s);
    usleep(120000); /* let the listener finish this connection before the next */
}

static void try_open(const char *name) {
    char buf[300];
    snprintf(buf, sizeof buf, "opening %s ...", name);
    pc_log(buf);
    void *h = dlopen(name, RTLD_LAZY);
    if (h) {
        snprintf(buf, sizeof buf, "  OK  %s loaded (%p)", name, h);
        pc_log(buf);
    } else {
        const char *e = dlerror();
        snprintf(buf, sizeof buf, "  NULL %s -> %s", name, e ? e : "(no dlerror)");
        pc_log(buf);
    }
}

int main(void) {
    pc_log("diag start");
    try_open("libSceRandom.sprx");        /* control: known to work in hello_dlfcn */
    try_open("libSceAppInstUtil.sprx");   /* the one that crashed the installer */
    try_open("libSceAppInstUtilServer.sprx");
    try_open("libSceAppInst.sprx");
    try_open("libSceBgft.sprx");          /* background file transfer (installer backend) */
    pc_log("diag done");
    return 0;
}
