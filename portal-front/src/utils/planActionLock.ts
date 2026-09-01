export interface PlanActionLock {
  tryAcquire: () => boolean
  release: () => void
  isLocked: () => boolean
}

export function createPlanActionLock(): PlanActionLock {
  let locked = false

  return {
    tryAcquire: () => {
      if (locked) return false
      locked = true
      return true
    },
    release: () => { locked = false },
    isLocked: () => locked,
  }
}

export async function runWithPlanActionLock(
  lock: PlanActionLock,
  action: () => Promise<void>,
): Promise<boolean> {
  if (!lock.tryAcquire()) return false

  try {
    await action()
    return true
  } finally {
    lock.release()
  }
}
