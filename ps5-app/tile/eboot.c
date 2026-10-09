/*
 * PKG MUTANT SHOP - tile eboot.
 * When you launch the "PKG MUTANT SHOP" tile on the PS5 dashboard, this runs and opens the app
 * (our companion web UI) in the PS5 browser. Built dynamically-linked against the system services.
 * Companion URL is baked in at build time: -DPMS_URL="http://<pc-ip>:8710"
 */
#include <stdio.h>
#include <unistd.h>

#ifndef PMS_URL
#define PMS_URL "http://10.0.0.76:8710"
#endif

int sceUserServiceInitialize(void *);
int sceSystemServiceLaunchWebBrowser(const char *uri, void *reserved);

int
main(void) {
    sceUserServiceInitialize(0);
    /* give the shell a moment after the app launches, then open our web UI */
    sleep(1);
    sceSystemServiceLaunchWebBrowser(PMS_URL, 0);
    return 0;
}
