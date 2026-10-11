# -*- coding: utf-8 -*-
"""Make the ps4-payload-sdk crt survive being loaded by GoldHEN.

WHY. The SDK targets its own ELF loader, which installs a `kexec` syscall the crt uses to reach
kernel memory. GoldHEN's loader does not provide that syscall - but it DOES jailbreak the host
process before jumping to our entry point ("found and jailbroken target process" in klog). So every
kernel step in the crt either fails or is redundant under GoldHEN, and because payload_init()
treats any failure as fatal, main() never runs: the payload is reported as "launched successfully"
and then does nothing. Measured exactly that, three times, before this patch.

Three changes, all narrowing "fatal" to "advisory" for kernel work only:
  1. payload_init(): kernel and patch init may fail; klog says so and the payload still starts.
  2. __rtld_init(): the prison/rootdir/jaildir escalation is skipped when the kernel is
     unreachable, because GoldHEN already did it. Symbol resolution is untouched.
  3. __rtld_init() tail: the jail is only RESTORED if we actually escalated. This one matters most
     and was the last blocker - the restore ran unconditionally, failed, set err=-1 and returned
     failure *after* the libraries had loaded fine.

Nothing here pretends the kernel is reachable when it is not; code that needs kernel read/write
must check for itself.

Each patch is guarded by its own unique marker. An earlier version of this script keyed every
guard on one shared marker, so patch 3 saw patch 2's marker and reported "already patched" without
doing anything - which cost a full test cycle. Exits non-zero if an anchor is missing, so a future
SDK update cannot be silently mis-patched.
"""
import io, sys, os

SDK = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/sdk/ps4-payload-sdk.tmp")
MARK = "/* PKG MUTANT SHOP: GoldHEN-tolerant crt */"


def patch(path, old, new, label, done_marker):
    """done_marker must be text unique to THIS replacement - never the shared MARK."""
    p = os.path.join(SDK, path)
    src = io.open(p, encoding="utf-8", newline="").read()
    if done_marker in src:
        print("  already applied: %s" % label)
        return 0
    if src.count(old) != 1:
        print("ANCHOR MISMATCH in %s (%s): found %d" % (path, label, src.count(old)))
        sys.exit(1)
    io.open(p, "w", encoding="utf-8", newline="").write(src.replace(old, new))
    print("  APPLIED: %s" % label)
    return 1


n = 0

n += patch("crt/crt.c",
           """  if((err=__kernel_init())) {
    return err;
  }
  if((err=__rtld_init())) {
    return err;
  }
  if((err=__patch_init())) {
    return err;
  }

  return err;""",
           """  """ + MARK + """
  /* GoldHEN loads us into a process it has ALREADY jailbroken and provides no kexec syscall, so
     these two cannot work and are not needed. Best-effort: on a loader that does provide kexec
     they still run and still give us kernel access. rtld stays fatal - symbol resolution is what
     every later call depends on. */
  if(__kernel_init()) {
    klog_puts("crt: no kernel access (expected under GoldHEN) - continuing");
  }
  if((err=__rtld_init())) {
    return err;
  }
  if(__patch_init()) {
    klog_puts("crt: kernel patches skipped - continuing");
  }

  return 0;""",
           "payload_init: kernel/patch advisory",
           "no kernel access (expected under GoldHEN)")

n += patch("crt/rtld.c",
           """  if(!(prison=kernel_get_ucred_prison(-1))) {
    klog_puts("kernel_get_ucred_prison failed");
    return -1;
  }
  if(!(rootdir=kernel_get_proc_rootdir(-1))) {
    klog_puts("kernel_get_proc_rootdir failed");
    return -1;
  }
  if(!(jaildir=kernel_get_proc_jaildir(-1))) {
    klog_puts("kernel_get_proc_jaildir failed");
    return -1;
  }

  if((err=kernel_set_proc_rootdir(-1, KERNEL_ADDRESS_ROOTVNODE))) {
    klog_puts("kernel_set_proc_rootdir failed");

  } else if((err=kernel_set_proc_jaildir(-1, KERNEL_ADDRESS_ROOTVNODE))) {
    klog_puts("kernel_set_proc_jaildir failed");

  } else if((err=kernel_set_ucred_prison(-1, KERNEL_ADDRESS_PRISON0))) {
    klog_puts("kernel_set_proc_rootdir failed");
  }""",
           """  """ + MARK + """
  /* Escaping the jail needs kernel access. Under GoldHEN we are outside it before our first
     instruction runs, so an unreachable kernel is the NORMAL case here and must not stop the
     payload. When the kernel IS reachable this behaves exactly as before. */
  prison  = kernel_get_ucred_prison(-1);
  rootdir = kernel_get_proc_rootdir(-1);
  jaildir = kernel_get_proc_jaildir(-1);
  if(!prison || !rootdir || !jaildir) {
    klog_puts("crt: jail escape skipped - the loader has already done it");
    err = 0;

  } else if((err=kernel_set_proc_rootdir(-1, KERNEL_ADDRESS_ROOTVNODE))) {
    klog_puts("kernel_set_proc_rootdir failed");

  } else if((err=kernel_set_proc_jaildir(-1, KERNEL_ADDRESS_ROOTVNODE))) {
    klog_puts("kernel_set_proc_jaildir failed");

  } else if((err=kernel_set_ucred_prison(-1, KERNEL_ADDRESS_PRISON0))) {
    klog_puts("kernel_set_ucred_prison failed");
  }""",
           "rtld_init: jail escape optional",
           "jail escape skipped - the loader has already done it")

n += patch("crt/rtld.c",
           """  if(kernel_set_proc_rootdir(-1, rootdir)) {
    klog_puts("kernel_set_proc_rootdir failed");
    err = -1;
  }
  if(kernel_set_proc_jaildir(-1, jaildir)) {
    klog_puts("kernel_set_proc_jaildir failed");
    err = -1;
  }
  if(kernel_set_ucred_prison(-1, prison)) {
    klog_puts("kernel_set_proc_rootdir failed");
    err = -1;
  }

  return err;""",
           """  """ + MARK + """
  /* Put the jail back only if we took it off. Without kernel access these three fail, set err=-1
     and __rtld_init returns failure AFTER the libraries loaded successfully - which is why main()
     still never ran once the escalation itself was made optional. Under GoldHEN there is nothing
     of ours to undo. */
  if(prison && rootdir && jaildir) {
    if(kernel_set_proc_rootdir(-1, rootdir)) {
      klog_puts("kernel_set_proc_rootdir failed");
      err = -1;
    }
    if(kernel_set_proc_jaildir(-1, jaildir)) {
      klog_puts("kernel_set_proc_jaildir failed");
      err = -1;
    }
    if(kernel_set_ucred_prison(-1, prison)) {
      klog_puts("kernel_set_ucred_prison failed");
      err = -1;
    }
  }

  return err;""",
           "rtld_init: restore only if we escalated",
           "nothing\n     of ours to undo")

print("patch_sdk_crt: %d change(s) applied" % n)
