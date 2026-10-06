/*
 * jb_escalate_pid — raise this process's credentials so the system app-installer
 * (sceAppInstUtil) accepts our install request.
 *
 * Verbatim from CheatRunner (github.com/notmaj0r/CheatRunner, src/jb.c, by maj0r),
 * which publishes it as the standard homebrew escalation helper. It only does anything
 * on an already-jailbroken console: every kernel_* call here uses the kernel read/write
 * that Y2JB already established. On a non-jailbroken system these functions no-op/fail.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include <ps5/kernel.h>

#include "jb.h"

#define JB_AUTHID 0x4801000000000013ULL

/* The authid is applied ONCE, at startup, before anything opens an IPC channel to ShellCore -
   and that ordering is the whole point of making it overridable here rather than per-call.
   sceAppInstUtilInitialize() establishes the installer IPC, and a credential swapped AFTER that
   plausibly never reaches ShellCore at all, so a per-call swap cannot rule the credential out.
   Measured on this console: etaHEN's Utility Daemon (the process whose base-game installs
   succeed) runs at 0x4800000000000006; we run at 0x4801000000000013.

   Reading it from a file keeps the experiment cheap: write the value, reload the ELF, and the
   whole process comes up on it with a fresh IPC. No file, or an unparseable one, means the
   long-standing JB_AUTHID - so a console that has never heard of this behaves exactly as before. */
#define JB_AUTHID_FILE "/data/pkg-mutant-shop/authid"

static unsigned long long
jb_authid_wanted(void) {
  FILE *f = fopen(JB_AUTHID_FILE, "r");
  if (!f) {
    return JB_AUTHID;
  }
  char buf[40] = {0};
  size_t n = fread(buf, 1, sizeof(buf) - 1, f);
  fclose(f);
  if (!n) {
    return JB_AUTHID;
  }
  char *end = 0;
  unsigned long long v = strtoull(buf, &end, 0);
  return v ? v : JB_AUTHID;
}

int
jb_escalate_pid(pid_t pid) {
  if (pid <= 0) {
    return -1;
  }

  if (!kernel_get_proc(pid)) {
    return -1;
  }

  int rc = 0;

  if (kernel_set_ucred_uid(pid, 0) != 0) rc = -1;
  if (kernel_set_ucred_ruid(pid, 0) != 0) rc = -1;
  if (kernel_set_ucred_svuid(pid, 0) != 0) rc = -1;
  if (kernel_set_ucred_rgid(pid, 0) != 0) rc = -1;
  if (kernel_set_ucred_svgid(pid, 0) != 0) rc = -1;

  intptr_t rootvnode = kernel_get_root_vnode();
  if (rootvnode) {
    if (kernel_set_proc_rootdir(pid, rootvnode) != 0) rc = -1;
    if (kernel_set_proc_jaildir(pid, rootvnode) != 0) rc = -1;
  }

  if (kernel_set_ucred_authid(pid, jb_authid_wanted()) != 0) rc = -1;

  uint8_t caps[16];
  memset(caps, 0xff, sizeof(caps));
  if (kernel_set_ucred_caps(pid, caps) != 0) rc = -1;

  /* cr_sceAttr byte 3 = 0x80 (ptrace). Verified on-device against Elf Arsenal's own
     privileged processes: their attrs are byte-for-byte identical to this, so do NOT
     set byte 0 as well — that is a deviation, not an upgrade. */
  uint8_t attrs[32];
  if (kernel_get_ucred_attrs(pid, attrs) == 0) {
    attrs[3] |= 0x80; /* ptrace */
    if (kernel_set_ucred_attrs(pid, attrs) != 0) rc = -1;
  } else {
    rc = -1;
  }

  return rc;
}
