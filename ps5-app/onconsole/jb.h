#pragma once

#include <sys/types.h>

/* Raise this process's credentials via the active jailbreak's kernel access so the
 * system app-installer accepts our install. From CheatRunner (by maj0r). */
int jb_escalate_pid(pid_t pid);
