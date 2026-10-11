/* OUR crt for the in-game agent .prx. Ours, not GoldHEN's - but it has to speak the platform's
 * plugin ABI exactly, or the loader will not load us cleanly.
 *
 * WHY THIS FILE EXISTS. The agent used to link OpenOrbis's crtlib.o, which is the startup for a
 * standalone homebrew ELF: it runs the full C runtime at load - init arrays, C++ constructors, the
 * lot - and it stamps the module's sce_module_param with an SDK version of 0x1000051. A GoldHEN
 * plugin is not a standalone ELF: it is a library the loader maps into a running game and then
 * calls into. Every plugin that loads cleanly (measured: game_patch and plugin_template, pulled off
 * this console) does the opposite of crtlib - it does NOTHING at load and stamps SDK 0x4508101.
 * Ours stamping 0x1000051 and running crtlib's heavy init at load is the difference between our
 * plugin and the ones that work, and load-time is exactly where the game was freezing.
 *
 * So this is a minimal crt, the shape a plugin needs and no more:
 *   - the sce_module_param the loader reads (size, the platform magic, the SDK version that the
 *     working plugins on this firmware carry). These three values are the Sony module ABI - any
 *     loadable module declares them - so matching them is speaking the platform's language, not
 *     borrowing anyone's code.
 *   - _init -> module_start and _fini -> module_stop, which in main.c do nothing and return 0, so
 *     NOTHING runs on the game's load path except the loader's own bookkeeping. Our real work
 *     happens later, when the loader calls plugin_load and our thread wakes after its delay.
 *
 * There is deliberately no libc initialisation here. libSceLibcInternal is a shared library already
 * live in the game process, so snprintf/memcpy/sockets resolve and work without our crt setting
 * anything up - which is exactly how the working plugins operate with their own tiny crt.
 */
#include <stddef.h>

extern int module_start(size_t args, const void *argp);
extern int module_stop(size_t args, const void *argp);

/* sce_module_param: size, platform magic, SDK version. The SDK version matches the plugins that
   load cleanly on this firmware; the magic is the fixed Sony process-param magic. */
__asm__(
    ".intel_syntax noprefix\n"
    ".align 0x8\n"
    ".section \".data.sce_module_param\"\n"
    "_sceProcessParam:\n"
    "  .quad 0x18\n"
    "  .quad 0x13C13F4BF\n"
    "  .quad 0x4508101\n"
    ".att_syntax prefix\n"
);

/* The two globals a C module is expected to carry. Both zero, like the working plugins. */
__asm__(
    ".intel_syntax noprefix\n"
    ".align 0x8\n"
    ".data\n"
    "__dso_handle:\n"
    "  .quad 0\n"
    "_sceLibc:\n"
    "  .quad 0\n"
    ".att_syntax prefix\n"
);

int _init(size_t args, const void *argp) { return module_start(args, argp); }
int _fini(size_t args, const void *argp) { return module_stop(args, argp); }
