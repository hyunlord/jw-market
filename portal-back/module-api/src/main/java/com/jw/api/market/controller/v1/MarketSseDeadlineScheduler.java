package com.jw.api.market.controller.v1;

import java.util.concurrent.TimeUnit;
import reactor.core.Disposable;
import reactor.core.scheduler.Schedulers;

@FunctionalInterface
interface MarketSseDeadlineScheduler {

    Disposable schedule(Runnable task, long delayMillis);

    static MarketSseDeadlineScheduler reactorParallel() {
        return (task, delayMillis) -> Schedulers.parallel().schedule(
            task,
            delayMillis,
            TimeUnit.MILLISECONDS);
    }
}
