#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "app/remote_key.h"

#define REMOTE_KEY_QUEUE_SIZE 16
#define REMOTE_KEY_MIN_HOLD_TICKS 3

typedef struct {
    KEY_Code_t key;
    uint8_t action;
} RemoteKeyEvent_t;

static RemoteKeyEvent_t gQueue[REMOTE_KEY_QUEUE_SIZE];
static uint8_t gHead;
static uint8_t gTail;
static uint8_t gDepth;

// State currently injected into the keyboard path.
static KEY_Code_t gInjectedKey = KEY_INVALID;
static uint8_t gInjectedHoldTicks;

// Predicted state after queued events are applied, used for enqueue-time validation.
static KEY_Code_t gPredictedKey = KEY_INVALID;

static bool IsAllowedKey(KEY_Code_t key)
{
    if (key >= KEY_INVALID)
        return false;

    // Do not allow virtual PTT over UART.
    if (key == KEY_PTT)
        return false;

    return true;
}

REMOTE_KEY_ACK_STATUS_t REMOTEKEY_Enqueue(KEY_Code_t key, uint8_t action)
{
    if (!IsAllowedKey(key))
        return REMOTE_KEY_ACK_INVALID;

    if (action != REMOTE_KEY_ACTION_PRESS && action != REMOTE_KEY_ACTION_RELEASE)
        return REMOTE_KEY_ACK_INVALID;

    if (action == REMOTE_KEY_ACTION_PRESS)
    {
        if (gPredictedKey != KEY_INVALID)
            return REMOTE_KEY_ACK_INVALID;
    }
    else
    {
        if (gPredictedKey != key)
            return REMOTE_KEY_ACK_INVALID;
    }

    if (gDepth >= REMOTE_KEY_QUEUE_SIZE)
        return REMOTE_KEY_ACK_BUSY;

    gQueue[gTail].key = key;
    gQueue[gTail].action = action;

    gTail = (uint8_t)((gTail + 1U) % REMOTE_KEY_QUEUE_SIZE);
    gDepth++;

    if (action == REMOTE_KEY_ACTION_PRESS)
        gPredictedKey = key;
    else
        gPredictedKey = KEY_INVALID;

    return REMOTE_KEY_ACK_ACCEPTED;
}

void REMOTEKEY_ProcessQueue(void)
{
    if (gInjectedHoldTicks > 0)
        gInjectedHoldTicks--;

    if (gDepth == 0)
        return;

    const RemoteKeyEvent_t *ev = &gQueue[gHead];

    if (ev->action == REMOTE_KEY_ACTION_PRESS)
    {
        gInjectedKey = ev->key;
        gInjectedHoldTicks = REMOTE_KEY_MIN_HOLD_TICKS;

        gHead = (uint8_t)((gHead + 1U) % REMOTE_KEY_QUEUE_SIZE);
        gDepth--;
        return;
    }

    // Release event: keep it queued until press is held long enough
    // so the debounced key path sees a stable press first.
    if (gInjectedHoldTicks > 0)
        return;

    gInjectedKey = KEY_INVALID;
    gHead = (uint8_t)((gHead + 1U) % REMOTE_KEY_QUEUE_SIZE);
    gDepth--;
}


uint8_t REMOTEKEY_GetQueueDepth(void)
{
    return gDepth;
}

KEY_Code_t REMOTEKEY_MergeWithHardware(KEY_Code_t hardware_key)
{
    // Physical keyboard has priority if actively pressed.
    if (hardware_key != KEY_INVALID)
        return hardware_key;

    return gInjectedKey;
}
