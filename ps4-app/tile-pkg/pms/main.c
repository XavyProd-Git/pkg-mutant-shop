/* PKG MUTANT SHOP - the PS4 home-screen app.
 *
 * WHAT IT IS. The icon on the PS4's home screen, and the PS4 twin of the PS5's dashboard tile.
 * Pressing it gets the shop running on this console and then opens it in the console's own browser.
 * The shop is a web app: the ELF hosts the page and the API on port 8710 from inside the console,
 * so it works with every PC switched off.
 *
 * WHY THE PS5 TILE IS SMALLER THAN THIS. On the PS5 a package can be metadata only: its param.json
 * carries `deeplinkUri` and the system opens that URL when the icon is pressed - no program at all.
 * The PS4 has no such field. A PS4 icon is a real application with a real eboot, so this program
 * exists to do by hand what the PS5 gets from one line of metadata.
 *
 * NOTHING IS LINKED THAT DOES NOT HAVE TO BE. This links libkernel alone and resolves everything
 * else at run time, which is the shape of the toolchain's own hello_world - the one sample known to
 * launch on this console family.
 *
 * CE-32930-7 WAS NEVER THIS FILE'S FAULT, AND IT IS NOW SOLVED ELSEWHERE.
 *
 * The console refused to start this app because BGFT had no *task* for the title:
 *
 *     [BGFT] ERROR: [3568] task not found. (PKGM00001)
 *     sceBgftNotifyGameWillStart() ret = 80990019
 *
 * The package's PFS images mounted fine; ShellCore unmounted them and gave up. The real cause was
 * ps4-app/onconsole/server_ps4.c handing its finished BGFT task back after every install - see
 * bgft_sweep_ours() there. With one task left registered, this app launches and runs.
 *
 * TWO EXPLANATIONS BLAMING THIS FILE WERE BOTH WRONG - first the SCE_NEEDED_MODULE list, then
 * narrowing the link line to libkernel alone. Do not add a third. If the icon ever stops opening
 * again, look at the BGFT task table before looking here.
 *
 * WHAT IS ACTUALLY WRONG IN HERE, measured on the first successful launch:
 *   - sceKernelLoadStartModule("/system/common/lib/...") fails for every library, because inside an
 *     application sandbox they are mapped at /vm2LJNGVpN/common/lib/. So browser, user_init and
 *     notify all resolved to 0 and the direct browser call was dead.
 *   - Asking the payload instead (GET /api/open) worked first time. The payload is not sandboxed.
 *     That fallback is the only reason anything reached the television.
 *   - Returning from main() raises SIGSYS - a system call this sandbox does not permit, from the
 *     toolchain's own exit path. That is ONE of the two ways out of here that ends in an error
 *     dialog, and the _Exit override below removes it.
 *   - THE OTHER ONE, and the one measured to be firing now, is the exit itself:
 *     sceSystemServiceLoadExec("exit", NULL) has the system kill this process and then FAIL to
 *     spawn the exit helper (0x80aa001a). See the long note above leave(). Do not read the SIGSYS
 *     paragraph as the explanation for a CE-34878-0 seen today without checking klog first - the
 *     same code on the television has had two different causes.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <orbis/libkernel.h>
#include <orbis/SystemService.h>

/* The one place the port is written down. The two notifications this app shows used to type
 * "127.0.0.1:8710" into their own sentence, so changing this line moved the server and left
 * the icon telling the owner to open the old address - on the one screen they are looking at
 * precisely because something is not where they expected it. */
#define SHOP_PORT 8710

/* A WRITE TO A SOCKET THE OTHER END HAS CLOSED KILLS THIS PROCESS, and that is very probably the
 * black screen followed by CE-34878-0.
 *
 * SIGPIPE's default action is to terminate. Every byte of the payload - 1.7 MB of it - goes out
 * through write() on a socket owned by the jailbreak's payload loader, and the moment that loader
 * decides it has enough (it saves the body to a temp file and then goes off to find and patch the
 * process it injects into) our next write lands on a closed pipe and this program is gone. No
 * message, no log line, no chance to notify: the signal is delivered and the process ends.
 *
 * The symptom matches exactly. The console showed the loader's own "payload received" notice - it
 * had the bytes - and then nothing at all, because the program that was supposed to wait for the
 * shop and open the browser had already been killed. And it is a race, which is why the same
 * build launched perfectly one day and died the next: the timing depends on what the console is
 * doing, and a reboot changes that.
 *
 * TWO LAYERS, because one of them may be refused:
 *   1. SIG_IGN for SIGPIPE, process-wide, before any socket exists. write() then returns -1/EPIPE,
 *      which every write_all() in here already handles as a failure.
 *   2. SO_NOSIGPIPE on each socket. FreeBSD's per-socket form of the same thing, and the PS4's
 *      network stack is FreeBSD. Defined here because the toolchain's headers may not carry it;
 *      the value is the FreeBSD one, and a kernel that does not know the option simply refuses
 *      the setsockopt, which is why its return value is deliberately not checked.
 *
 * This project has been bitten by SIGPIPE on this console before, in the payload. The payload
 * ignores it. This program - which does the single largest write anything here performs - did not.
 */
#ifndef SO_NOSIGPIPE
#define SO_NOSIGPIPE 0x0800
#endif

static void sock_no_sigpipe(int s) {
    int one = 1;
    (void)setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
}

/* ------------------------------------------------- calling things, and leaving without crashing
 *
 * NOTHING IS RESOLVED AT RUN TIME ANY MORE, because run-time resolution by absolute path cannot
 * work from inside an application sandbox and the first successful launch proved it:
 *
 *     [PMS] load /system/common/lib/libSceSystemService.sprx -> handle=-2147352574 res=0x00000000
 *     [PMS] resolved: browser=0 user_init=0 notify=0
 *
 * -2147352574 is 0x80020002, ORBIS_KERNEL_ERROR_ENOENT. The crash dump named the real location:
 * this process had its system libraries mapped at /vm2LJNGVpN/common/lib/ - a per-sandbox random
 * prefix - so /system/common/lib is simply not a path an application can see. Linking is how a
 * sandboxed app reaches these functions; the loader then resolves them from wherever the sandbox
 * actually keeps them.
 *
 * sceKernelSendNotificationRequest never needed any of that: it is a libkernel export declared in
 * orbis/libkernel.h, and -lkernel was on the link line the whole time. It was being looked up
 * through a handle that could never open.
 *
 * THE BROWSER CALL IS GONE. The toolchain declares `void sceSystemServiceLaunchWebBrowser();` -
 * no url, no return - so the four-argument call this file used to make was a guess dressed up with
 * a comment. Asking the shop's payload instead is measured working on this console (`/api/open`
 * answered 200 and the browser came forward), and the payload is not sandboxed. One route that
 * works beats two where one is invented.
 */

/* THE ICON FIELD STAYS ZERO. `useIconImageUri = 1` picks the form that draws an icon beside the
   text, and on this console family that form returns success and renders NOTHING - this project has
   been caught by it twice on the PS5. The shop's payload sends the plain form, measured working on
   this exact console, so this sends the identical thing. */
static void notify(const char *text) {
    OrbisNotificationRequest req;
    memset(&req, 0, sizeof(req));
    snprintf(req.message, sizeof(req.message), "%s", text);
    sceKernelSendNotificationRequest(0, &req, sizeof(req), 0);
}

/* ---- WHY THIS APP USED TO CRASH ON THE WAY OUT, and the two things that stop it
 *
 * The television showed an error to dismiss (CE-34878-0) every time, AFTER the shop had already
 * opened. This was ONE of its two causes and it is fixed; the other is the exit spawn, and the
 * note above leave() has that one with its own klog. klog for THIS one:
 *
 *     # A user thread receives a fatal signal
 *     # signal: 12 (SIGSYS)   thread name: eboot.bin
 *     # rip: 00000008000028bc   BrF: 0000000000406c20   BrT: 00000008000028b0
 *
 * SIGSYS is a system call this process is not allowed to make. The two branch registers say where
 * from: BrF 0x406c20 is in this eboot's own text (the PLT), BrT 0x8000028b0 is in libkernel, and
 * the fault is twelve bytes further in. That is the chain `return` out of main -> the CRT's exit ->
 * _Exit -> _exit@plt -> libkernel's _exit -> its syscall, refused. An application sandbox does not
 * get to call it.
 *
 * TWO LAYERS, and the first is what actually guarantees it:
 *
 *   1. DEFINE _Exit HERE. The toolchain's libc keeps _Exit, exit and _exit in three separate
 *      archive members (verified: `ar t libc.a` lists _Exit.lo, exit.lo, _exit.lo), so our
 *      definition satisfies exit's reference to _Exit and _exit.lo is never pulled in at all. The
 *      faulting instruction is not merely unreached, it is not in the binary. No stray exit(),
 *      abort() or `return` from main can find it again.
 *   2. LEAVE THE WAY AN APPLICATION IS MEANT TO. sceSystemServiceLoadExec("exit", NULL) is the
 *      documented way for a title to close itself and hand the user back to the system.
 *
 * If LoadExec is refused this parks instead of exiting. Parking is not lovely - the app sits
 * suspended and shows nothing if you navigate back to it, and it has to be closed with the PS
 * button - but it is quiet, and a silent oddity beats an error dialog after every single launch.
 * The return code is logged so the next person knows which of the two actually happened. */
static void park_forever(void) {
    for (;;) sceKernelUsleep(60 * 1000000);
}

/* ---- CE-34878-0 IS THE EXIT, AND NOW IT IS MEASURED RATHER THAN REASONED ABOUT
 *
 * klog, captured on a real press of the icon (2026-09-23), immediately after the shop answered
 * /api/open and the browser came forward - so the app had already done its whole job:
 *
 *     [Syscore App] Kill for LoadExec(0x6c)
 *     [Syscore App] Kill for LoadExec(0x6c) => 0
 *     [AppMgr Trace]: pid=0x6c, deleted.
 *     [AppMgr] Executing next spawn
 *     [Syscore App] processParam: elfPath = exit, fullPath = .../PKGM00001-app0-patch0-union/eboot.bin
 *     [Syscore App] processSpawn() error: 0x80aa001a
 *     [AppMgr] Exit spawn failed : 0x80aa001a  ELF path=exit
 *
 * So `sceSystemServiceLoadExec("exit", NULL)` does not fail politely and return a code we can act
 * on. The system KILLS this process first and only then fails to spawn the exit helper - which is
 * the error on the television. Both of the fallbacks written below it were therefore dead code:
 * neither the printf nor park_forever() can run, because by then there is no process left.
 *
 * sceSystemServiceKillLocalProcess() WAS TRIED AND IS REFUSED. Do not try it again. It is
 * declared no-argument in the toolchain's own include/orbis/SystemService.h, so there was nothing
 * to get wrong about the call - and the console answered, in words, that we are not allowed to
 * make it (klog, real icon press, 2026-09-23):
 *
 *     [PMS] done - closing this app
 *     [SceLncService] Bug#16557 !isSystemProcess(pid=0x73) is not allowed to invoke this API.
 *     begin_killLocalProcess: LNC_CHECK::0x8094000f (isSystemProcess(session) value is false)
 *     sceSystemServiceKillLocalProcess: SYSTEM_SERVICE_ISOK::0x8094000f
 *
 * It failed cleanly - returned, changed nothing - and the LoadExec below then failed exactly as it
 * always does. So BOTH documented ways out of an application are closed to this app on this
 * firmware, and the error dialog after the shop opens is the cost of that, not a bug in this file.
 *
 * WHAT IS LEFT, and why it has not been taken unilaterally: park_forever() produces no dialog at
 * all, because nothing kills us and nothing is spawned in our place. Two things are unknown about
 * it and both need the console to answer them - whether pressing the icon again RESUMES a parked
 * app to a black screen instead of restarting it, and whether an application that never exits
 * interferes with rest mode. Neither is worth guessing at on someone else's console to remove a
 * dialog they can dismiss. If it is taken, the resume case has an answer that needs no new API:
 * sceKernelGettimeofday() across a one-second sceKernelUsleep() shows a multi-second jump when the
 * process has been suspended and brought back, which is the moment to ask the shop to open again.
 *
 * A THIRD THEORY WAS CHECKED AND IS DEAD. The failing spawn names
 * ".../PKGM00001-app0-patch0-union/eboot.bin", which looked like the app running from a patch
 * union that the exit helper cannot be found in - repeated installs leaving a patch behind.
 * /user/patch/PKGM00001 does not exist on this console; that path is simply how SceShellCore names
 * the union for every title. */
_Noreturn void _Exit(int ec) {
    (void)ec;
    /* No printf: stdio has already been torn down by the time exit() reaches here. */
    sceSystemServiceLoadExec("exit", NULL);
    park_forever();
}

static _Noreturn void leave(void) {
    fflush(stdout);
    printf("[PMS] done - closing this app\n");
    /* The kill-local-process call that used to be here is gone: measured refused, see above. It
       cost nothing but a line in klog and it achieved nothing, and a call that is known not to
       work is worse than none - the next person reads it as an attempt that might be helping. */
    int rc = (int)sceSystemServiceLoadExec("exit", NULL);
    /* Not reached today: the system kills this process to perform the LoadExec and only then
       fails to spawn the exit helper. Kept because it costs nothing and because a firmware that
       does spawn it would return here normally. */
    printf("[PMS] still here: LoadExec(exit) rc=0x%08x - parking instead\n", (unsigned)rc);
    park_forever();
}

/* ---------------------------------------------------------------- the shop */

static int shop_is_up(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return 0;
    sock_no_sigpipe(s);
    struct timeval tv;
    tv.tv_sec = 2; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(SHOP_PORT);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    int rc = connect(s, (struct sockaddr *)&a, sizeof(a));
    close(s);
    return rc == 0;
}

static int write_all(int fd, const char *buf, size_t len);   /* defined with the loader below */

/* ASK THE SHOP TO OPEN IT, when the shop is up.
 *
 * The payload already has /api/open, and it runs in an UNSANDBOXED process where this project has
 * measured the browser call working. This program is an application in a sandbox, and whether
 * /system/common/lib is even visible from there is not established - if those module handles come
 * back empty, a browser call from here does nothing and the icon looks dead.
 *
 * So when the shop is answering, hand it the job: one socket, no system module needed, and the same
 * code path the phone and the PC already use to open the shop on the television. The direct call
 * below stays as the fallback for when there is no shop to ask. */
static int ask_shop_to_open(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return -1;
    sock_no_sigpipe(s);
    struct timeval tv;
    tv.tv_sec = 15; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(SHOP_PORT);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) { close(s); return -1; }
    const char *req = "GET /api/open HTTP/1.1\r\n"
                      "Host: 127.0.0.1\r\n"
                      "Connection: close\r\n\r\n";
    if (write_all(s, req, strlen(req)) != 0) { close(s); return -1; }
    char rep[512] = {0};
    ssize_t n = read(s, rep, sizeof(rep) - 1);
    close(s);
    if (n <= 0) return -1;
    printf("[PMS] the shop answered /api/open: %.120s\n", rep);
    /* "launched":true is the shop saying the console took it. Anything else and we try ourselves. */
    return strstr(rep, "\"launched\":true") ? 0 : -1;
}

/* IS THE JAILBREAK LOADED, even if its payload loader is not?
 *
 * GoldHEN's FTP server is the tell. It comes up with the jailbreak and, unlike the payload loader
 * on 9090, it SURVIVES a rest/resume - which is exactly the state that produces the confusing
 * failure this exists to name. An ordinary FTP server is safe to open a connection to and close
 * again; nothing here does that to 9090, and nothing here ever should.
 *
 * Both ports, because which one answers depends on which jailbreak is loaded: 2121 is GoldHEN's,
 * 1337 is what etaHEN-style setups use. Either one means "the console is jailbroken right now".
 */
static int jailbreak_is_loaded(void) {
    static const int ports[] = { 2121, 1337 };
    for (int i = 0; i < 2; i++) {
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) continue;
        sock_no_sigpipe(s);
        struct timeval tv;
        tv.tv_sec = 1; tv.tv_usec = 0;
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port = htons((unsigned short)ports[i]);
        a.sin_addr.s_addr = inet_addr("127.0.0.1");
        int rc = connect(s, (struct sockaddr *)&a, sizeof(a));
        close(s);
        if (rc == 0) return ports[i];
    }
    return 0;
}

/* ------------------------------------------------------- starting the shop

   The package carries the shop's payload at /app0. A payload loader is what turns those bytes into
   a running process, and a jailbroken PS4 has one of two: the common one speaks HTTP, the other
   takes the bare ELF with no framing. Try both; one refused connect is all it costs.

   NEVER OPEN A CONNECTION THAT IS NOT THEN FILLED WITH THE WHOLE PAYLOAD. A loader handed an empty
   connection can stop listening - this console lost its loader twice that way, from nothing more
   than a port scan. That is also why there is no "is a loader there?" probe: the connect below IS
   the probe and it is always followed by the payload. */
#define PAYLOAD_PATH "/app0/pms-payload.elf"
#define LOADER_HTTP  9090
#define LOADER_RAW   9021

static int write_all(int fd, const char *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(fd, buf + off, len - off);
        if (w <= 0) return -1;
        off += (size_t)w;
    }
    return 0;
}

static int send_payload(int port, int http) {
    struct stat st;
    if (stat(PAYLOAD_PATH, &st) != 0 || st.st_size <= 0) return -1;
    int f = open(PAYLOAD_PATH, O_RDONLY);
    if (f < 0) return -1;

    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { close(f); return -1; }
    sock_no_sigpipe(s);
    struct timeval tv;
    tv.tv_sec = 30; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) { close(s); close(f); return -1; }

    if (http) {
        char hdr[256];
        int hn = snprintf(hdr, sizeof(hdr),
                          "POST / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                          "Content-Type: application/octet-stream\r\n"
                          "Content-Length: %lld\r\nConnection: close\r\n\r\n",
                          port, (long long)st.st_size);
        if (write_all(s, hdr, (size_t)hn) != 0) { close(s); close(f); return -1; }
    }
    char *buf = (char *)malloc(64 * 1024);
    if (!buf) { close(s); close(f); return -1; }
    int bad = 0;
    for (;;) {
        ssize_t r = read(f, buf, 64 * 1024);
        if (r < 0) { bad = 1; break; }
        if (r == 0) break;
        if (write_all(s, buf, (size_t)r) != 0) { bad = 1; break; }
    }
    free(buf);
    close(f);
    /* The loader is not an HTTP server and usually just closes, so a long wait here buys nothing
       and costs the user a black screen - this program draws nothing once the splash is hidden.
       Five seconds is long enough for a reply that is coming and short enough not to be felt. */
    struct timeval rtv;
    rtv.tv_sec = 5; rtv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &rtv, sizeof(rtv));
    char rep[256] = {0};
    if (!bad) {
        ssize_t rn = read(s, rep, sizeof(rep) - 1);
        if (rn > 0) {
            for (ssize_t i = 0; i < rn; i++)
                if (rep[i] == '\r' || rep[i] == '\n') { rep[i] = 0; break; }
            printf("[PMS] loader :%d replied: %.80s\n", port, rep);
        }
    }
    close(s);
    /* SENT, not "started". All this can know is that every byte left this program; whether the
       loader could do anything with them is answered by the shop coming up, below. Calling this
       "took the payload" read as success and hid a hand-over that had gone nowhere. */
    printf("[PMS] loader :%d - payload sent in full: %s (%lld bytes)\n",
           port, bad ? "no" : "yes", (long long)st.st_size);
    return bad ? -1 : 0;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    /* BEFORE ANY SOCKET EXISTS. See the note beside sock_no_sigpipe: an unhandled SIGPIPE ends
       this process instantly and silently, part-way through handing over the payload. */
    signal(SIGPIPE, SIG_IGN);
    printf("[PMS] PKG MUTANT SHOP app starting\n");
    /* WITHOUT THIS THE CONSOLE SHOWS THE LOADING SCREEN AND NOTHING ELSE, which from the sofa is
       indistinguishable from an app that does not work. The toolchain's own samples call it first
       thing; ours never did - and until this build it was called through a pointer that was always
       NULL, so it has never actually run. */
    sceSystemServiceHideSplashScreen();

    int up = 0;
    for (int i = 0; i < 3 && !up; i++) {
        up = shop_is_up();
        if (!up) sceKernelUsleep(1200 * 1000);
    }

    if (up) {
        printf("[PMS] the shop is answering - opening it\n");
        notify("Opening PKG MUTANT SHOP");
        /* ASKING THE SHOP IS THE ONLY ROUTE. It is not sandboxed and this is measured working on
           the hardware; the app's own browser call was a guessed signature and is gone. */
        if (ask_shop_to_open() != 0)
            { char _m[128];
              snprintf(_m, sizeof(_m),
                       "PKG MUTANT SHOP is running\nOpen 127.0.0.1:%d in the browser", SHOP_PORT);
              notify(_m); }
        leave();
    }

    /* NOT RUNNING - so start it. The package carries the shop, and handing it to a payload
       loader is the one way an application that is not privileged can get code into a process that
       outlives it. It has to outlive us: the moment the browser comes forward this app is
       suspended, so a shop served from HERE would stop answering exactly when the page loaded. */
    printf("[PMS] nothing on :%d - starting the shop\n", SHOP_PORT);
    notify("Starting PKG MUTANT SHOP");

    /* TWO ROUNDS. Pressing the icon seconds after the exploit lands can find the loader still
       coming up, and the whole cost of being wrong about that is one refused connect and two
       seconds. The payload is re-read from /app0 each time, so a half-sent first attempt cannot
       leave the second one sending the tail of it. */
    int handed = 0;
    for (int round = 0; round < 2 && !handed; round++) {
        if (round) sceKernelUsleep(2 * 1000000);
        handed = (send_payload(LOADER_HTTP, 1) == 0) || (send_payload(LOADER_RAW, 0) == 0);
    }
    /* THE TELEVISION IS THE ONLY DISPLAY THIS PROGRAM HAS. The splash is hidden and it draws
       nothing, so between here and the browser coming forward the screen is black - and a black
       screen is what a crash looks like too. One line, once, so the wait is visibly a wait. */
    if (handed) notify("PKG MUTANT SHOP is starting\nThis takes a few seconds");
    if (handed) {
        for (int i = 0; i < 30; i++) {
            sceKernelUsleep(1000 * 1000);
            if (shop_is_up()) {
                printf("[PMS] the shop came up after %d second(s)\n", i + 1);
                notify("Opening PKG MUTANT SHOP");
                if (ask_shop_to_open() != 0)
                    { char _m[128];
                      snprintf(_m, sizeof(_m),
                               "PKG MUTANT SHOP is running\nOpen 127.0.0.1:%d in the browser", SHOP_PORT);
                      notify(_m); }
                leave();
            }
        }
        notify("PKG MUTANT SHOP did not finish starting\n"
               "Try opening this again in a moment");
        leave();
    }

    /* TWO FAILURES THAT LOOK IDENTICAL FROM THE SOFA, told apart by one question: is the
       jailbreak loaded at all? See jailbreak_is_loaded(). The second case is the common one after
       a rest/resume and the owner can fix it in seconds once it is named. */
    int jb = jailbreak_is_loaded();
    printf("[PMS] no payload loader answered; jailbreak %s\n",
           jb ? "IS loaded (its payload loader is not)" : "is NOT loaded");
    if (jb)
        notify("PKG MUTANT SHOP cannot start\n"
               "The jailbreak is running but its payload loader is not - this happens after rest "
               "mode. Run the jailbreak again, then open this.");
    else
        notify("PKG MUTANT SHOP cannot start\n"
               "This PS4 is not jailbroken right now - run the jailbreak, then open this.");
    sceKernelUsleep(8 * 1000000);
    leave();
}
