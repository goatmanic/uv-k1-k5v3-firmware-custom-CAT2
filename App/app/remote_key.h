#ifndef APP_REMOTE_KEY_H
#define APP_REMOTE_KEY_H

#include <stdint.h>

#include "driver/keyboard.h"

enum REMOTE_KEY_ACK_STATUS_e {
    REMOTE_KEY_ACK_ACCEPTED = 0,
    REMOTE_KEY_ACK_BUSY     = 1,
    REMOTE_KEY_ACK_INVALID  = 2,
    REMOTE_KEY_ACK_STALE    = 3,
};
typedef enum REMOTE_KEY_ACK_STATUS_e REMOTE_KEY_ACK_STATUS_t;

enum REMOTE_KEY_ACTION_e {
    REMOTE_KEY_ACTION_PRESS   = 0,
    REMOTE_KEY_ACTION_RELEASE = 1,
};
typedef enum REMOTE_KEY_ACTION_e REMOTE_KEY_ACTION_t;

REMOTE_KEY_ACK_STATUS_t REMOTEKEY_Enqueue(KEY_Code_t key, uint8_t action);
void REMOTEKEY_ProcessQueue(void);
uint8_t REMOTEKEY_GetQueueDepth(void);
KEY_Code_t REMOTEKEY_MergeWithHardware(KEY_Code_t hardware_key);

#endif
