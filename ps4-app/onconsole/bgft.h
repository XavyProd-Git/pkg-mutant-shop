/* BGFT - the PS4's background file transfer service, which is how a package is installed from a
   URL: we register a download task pointing at the companion's HTTP server and start it, and the
   console does the downloading and installing itself, with its own progress UI.

   WHY THIS FILE EXISTS AND WHERE IT COMES FROM. Guessing a system call's signature is the one
   mistake this project has already paid for once (inferring a cancel call took a console down), so
   nothing here is inferred. Every declaration below is the canonical one published in the
   OpenOrbis PS4 toolchain (include/orbis/Bgft.h and _types/bgft.h, which in turn come from flatz's
   stub-library work), and every symbol named here was separately confirmed to EXIST on this
   console by resolving it at runtime before any of it was called - see
   ps4-app/onconsole/README.md and the memory note ps4-port-hardware-facts.

   One firmware detail that matters: on 13.52 libSceBgft exports the "ServiceInt" family. The older
   sceBgftInitialize / sceBgftDownloadRegisterTask names that most PS4 installers use are NOT
   present on this firmware - resolving them returns nothing - so those are deliberately absent
   here. We resolve everything with dlsym rather than linking, because there is no libSceBgft stub
   in the ps4-payload SDK, and because a missing symbol must be a clean refusal rather than a
   payload that will not load.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>

typedef int32_t OrbisBgftTaskId;
#define BGFT_INVALID_TASK_ID (-1)

typedef struct {
    void  *heap;
    size_t heapSize;
} OrbisBgftInitParams;

/* Task option bits. DISABLE_CDN_QUERY_PARAM is what the reference installer passes for a package
   that does not come from the store, which is exactly our case. */
enum {
    ORBIS_BGFT_TASK_OPT_NONE                    = 0x0,
    ORBIS_BGFT_TASK_OPT_DELETE_AFTER_UPLOAD     = 0x1,
    ORBIS_BGFT_TASK_OPT_INVISIBLE               = 0x2,
    ORBIS_BGFT_TASK_OPT_ENABLE_PLAYGO           = 0x4,
    ORBIS_BGFT_TASK_OPT_FORCE_UPDATE            = 0x8,
    ORBIS_BGFT_TASK_OPT_REMOTE                  = 0x10,
    ORBIS_BGFT_TASK_OPT_DISABLE_INSERT_POPUP    = 0x40,
    ORBIS_BGFT_TASK_OPT_INTERNAL                = 0x80,      /* ignores release date */
    ORBIS_BGFT_TASK_OPT_DISABLE_CDN_QUERY_PARAM = 0x10000,
};

/* Sub-types, for reference when reading a task back. */
enum {
    ORBIS_BGFT_TASK_SUB_TYPE_GAME       = 6,
    ORBIS_BGFT_TASK_SUB_TYPE_GAME_AC    = 7,
    ORBIS_BGFT_TASK_SUB_TYPE_GAME_PATCH = 8,
    ORBIS_BGFT_TASK_SUB_TYPE_PACKAGE    = 12,
};

/* FIELD ORDER IS LOAD-BEARING - this is a struct the firmware reads, not one of ours. Note the
   two fields most homebrew forgets: contentExUrl and skuId sit between the url and the icon, and
   `option` is a 32-bit enum between two pointers (so the compiler's natural padding after it is
   correct and must not be "fixed"). */
typedef struct {
    int32_t     userId;
    int32_t     entitlementType;
    const char *id;                 /* content id, max 0x30  */
    const char *contentUrl;         /* max 0x800             */
    const char *contentExUrl;
    const char *contentName;        /* max 0x259             */
    const char *iconPath;           /* max 0x800             */
    const char *skuId;
    int32_t     option;             /* OrbisBgftTaskOpt      */
    const char *playgoScenarioId;
    const char *releaseDate;
    const char *packageType;
    const char *packageSubType;
    uint32_t    packageSize;
} OrbisBgftDownloadParam;

typedef struct {
    uint32_t bits;
    int32_t  errorResult;
    uint32_t length;
    uint32_t transferred;
    uint32_t lengthTotal;
    uint32_t transferredTotal;
    uint32_t numIndex;
    uint32_t numTotal;
    uint32_t restSec;
    uint32_t restSecTotal;
    int32_t  preparingPercent;
    int32_t  localCopyPercent;
} OrbisBgftTaskProgress;

/* The exact set this firmware exports, with the exact signatures. Resolved by name at run time. */
typedef int32_t (*pfn_bgft_init_t)(OrbisBgftInitParams *params);
typedef int32_t (*pfn_bgft_term_t)(void);
typedef int32_t (*pfn_bgft_register_t)(OrbisBgftDownloadParam *params, OrbisBgftTaskId *taskId);
typedef int32_t (*pfn_bgft_start_t)(OrbisBgftTaskId taskId);
typedef int32_t (*pfn_bgft_stop_t)(OrbisBgftTaskId taskId);
typedef int32_t (*pfn_bgft_unregister_t)(OrbisBgftTaskId taskId);
typedef int32_t (*pfn_bgft_progress_t)(OrbisBgftTaskId taskId, OrbisBgftTaskProgress *progress);
