package com.example.antitheft

import android.content.Context
import android.util.Log
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * WorkManager 周期任务：每 15 分钟拉一次指令队列（纯轮询唤醒核心）。
 * 拉到 activate/locate/photo/lock/wipe 就转交给 AntiTheftService 执行。
 */
class PollWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {

    override fun doWork(): Result {
        val token = Prefs.getToken(applicationContext) ?: return Result.success()
        return try {
            // poll 一次返回该设备全部待执行命令（最坏只等一个 15 分钟周期，不再 N×15）
            val cmds = RelayApi.poll(token)
            for (c in cmds) {
                Log.i("PollWorker", "cmd=${c.cmd}")
                CommandDispatcher.dispatch(applicationContext, c.cmd, c.params)
            }
            Result.success()
        } catch (e: Exception) {
            Log.e("PollWorker", "poll ex", e)
            Result.retry()
        }
    }

    companion object {
        private const val NAME = "at_poll"

        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<PollWorker>(C.POLL_INTERVAL_MIN, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                NAME, ExistingPeriodicWorkPolicy.UPDATE, req
            )
        }
    }
}
