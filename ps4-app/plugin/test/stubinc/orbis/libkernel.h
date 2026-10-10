/* A stand-in for the PS4 SDK's <orbis/libkernel.h>, for ONE purpose: compiling the real
 * ps4-app/plugin/source/main.c on this PC so its wire protocol can be run against the real client
 * in ps4-app/onconsole/server_ps4.c. See ps4-app/plugin/test/harness.c.
 *
 * EVERY DECLARATION HERE WAS COPIED OUT OF THE TOOLCHAIN'S OWN HEADERS, not written from memory -
 * include/orbis/libkernel.h and include/orbis/_types/kernel.h of the OpenOrbis checkout. That is
 * the whole value of the file: a stub that drifts from the real signatures would make the test
 * agree with itself and not with the console. The layouts matter as much as the names, because
 * main.c reads OrbisAppInfo by byte offset and walks OrbisKernelModuleInfo's segment array.
 *
 * Nothing here is ever compiled for the PS4. The real build (build-wsl.sh) passes -isystem into
 * the toolchain and never sees this directory.
 */
#ifndef PMS_TEST_STUB_LIBKERNEL_H
#define PMS_TEST_STUB_LIBKERNEL_H

#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include <pthread.h>

/* _types/kernel.h */
typedef unsigned char vm_prot_t;
#define VM_PROT_NONE    ((vm_prot_t)0x00)
#define VM_PROT_READ    ((vm_prot_t)0x01)
#define VM_PROT_WRITE   ((vm_prot_t)0x02)
#define VM_PROT_EXECUTE ((vm_prot_t)0x04)
#define VM_PROT_ALL     (VM_PROT_READ | VM_PROT_WRITE | VM_PROT_EXECUTE)

typedef struct OrbisKernelModuleSegmentInfo {
    void    *address;
    uint32_t size;
    int32_t  prot;
} OrbisKernelModuleSegmentInfo;

typedef struct OrbisKernelModuleInfo {
    size_t                       size;
    char                         name[256];
    OrbisKernelModuleSegmentInfo segmentInfo[4];
    uint32_t                     segmentCount;
    uint8_t                      fingerprint[20];
} OrbisKernelModuleInfo;

typedef uint32_t OrbisKernelModule;

/* TitleId lands at offset 16 by this layout - which is what main.c reads, and what was measured
   on the console. The stub therefore CONFIRMS the offset rather than just going along with it. */
typedef struct {
    int32_t AppId;
    int32_t Unk;
    char    unk0x8[0x4];
    int32_t AppType;
    char    TitleId[10];
    char    unk0x1A[0x2E];
} OrbisAppInfo;

/* _types/pthread.h */
typedef pthread_t OrbisPthread;
typedef void      OrbisPthreadAttr;      /* main.c only ever passes NULL */

/* _types/kernel.h:176 */
typedef mode_t OrbisKernelMode;

/* libkernel.h - the file calls. These are what the agent uses instead of libc stdio, because stdio
   inside a game is what was crashing it: a build importing only fopen/fwrite/fclose crashed Dark
   Souls II, and the same build with no imports at all loaded and played. game_patch - a plugin this
   console loads cleanly, doing real file work - uses exactly these. */
int32_t sceKernelOpen(const char *, int32_t, OrbisKernelMode);
size_t  sceKernelRead(int32_t, void *, size_t);
size_t  sceKernelWrite(int32_t, const void *, size_t);
int32_t sceKernelClose(int32_t);
off_t   sceKernelLseek(int32_t, off_t, int);

int32_t sceKernelGetAppInfo(pid_t pid, OrbisAppInfo *info);
int32_t sceKernelGetModuleList(OrbisKernelModule *array, size_t size, size_t *available);
int32_t sceKernelGetModuleInfo(OrbisKernelModule handle, OrbisKernelModuleInfo *info);
int32_t sceKernelMprotect(const void *, size_t, int);
int32_t sceKernelUsleep(uint32_t);
int32_t scePthreadCreate(OrbisPthread *, const OrbisPthreadAttr *, void *(*F)(void *),
                         void *, const char *);
int32_t scePthreadDetach(OrbisPthread);

#endif
