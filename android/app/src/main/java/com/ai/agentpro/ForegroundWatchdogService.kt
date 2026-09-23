package com.ai.agentpro

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import android.util.Log
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class ForegroundWatchdogService : Service() {

    companion object {
        private const val TAG = "AndroidAgentPro.Watchdog"

        private const val CHANNEL_ID = "android_agent_pro_watchdog"
        private const val CHANNEL_NAME = "AndroidAgentPro Watchdog"
        private const val CHANNEL_DESCRIPTION =
            "Monitors the AndroidAgentPro local bridge"

        private const val NOTIFICATION_ID = 8070

        private const val ACTION_START =
            "com.ai.agentpro.action.WATCHDOG_START"
        private const val ACTION_STOP =
            "com.ai.agentpro.action.WATCHDOG_STOP"

        private const val WATCHDOG_INTERVAL_SECONDS = 15L
        private const val BRIDGE_RESTART_COOLDOWN_MS = 30_000L

        @Volatile
        private var instance: ForegroundWatchdogService? = null

        fun start(context: Context) {
            val intent = Intent(
                context,
                ForegroundWatchdogService::class.java
            ).setAction(ACTION_START)

            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }

                Log.i(TAG, "Watchdog start requested")
            } catch (exception: Exception) {
                Log.e(
                    TAG,
                    "Unable to start watchdog service",
                    exception
                )
            }
        }

        fun stop(context: Context) {
            val intent = Intent(
                context,
                ForegroundWatchdogService::class.java
            ).setAction(ACTION_STOP)

            try {
                context.startService(intent)
                Log.i(TAG, "Watchdog stop requested")
            } catch (exception: Exception) {
                Log.e(
                    TAG,
                    "Unable to request watchdog stop",
                    exception
                )
            }
        }

        fun isRunning(): Boolean {
            return instance != null
        }
    }

    private val serviceRunning = AtomicBoolean(false)

    private var scheduler: ScheduledExecutorService? = null

    @Volatile
    private var lastBridgeRestartAt = 0L

    override fun onCreate() {
        super.onCreate()

        instance = this

        try {
            createNotificationChannel()

            startForeground(
                NOTIFICATION_ID,
                createNotification()
            )

            serviceRunning.set(true)

            startWatchdogLoop()

            Log.i(
                TAG,
                "Foreground watchdog created"
            )
        } catch (exception: Exception) {
            serviceRunning.set(false)

            Log.e(
                TAG,
                "Unable to initialize foreground watchdog",
                exception
            )

            if (instance === this) {
                instance = null
            }

            stopSelf()
        }
    }

    override fun onStartCommand(
        intent: Intent?,
        flags: Int,
        startId: Int
    ): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                Log.i(
                    TAG,
                    "Stop action received"
                )

                stopWatchdog()
                stopSelf()

                return START_NOT_STICKY
            }

            ACTION_START,
            null -> {
                if (!serviceRunning.get()) {
                    try {
                        startForeground(
                            NOTIFICATION_ID,
                            createNotification()
                        )

                        serviceRunning.set(true)
                        startWatchdogLoop()

                        Log.i(
                            TAG,
                            "Watchdog start action processed"
                        )
                    } catch (exception: Exception) {
                        Log.e(
                            TAG,
                            "Unable to process watchdog start",
                            exception
                        )

                        stopSelf()

                        return START_NOT_STICKY
                    }
                }
            }

            else -> {
                Log.w(
                    TAG,
                    "Unknown watchdog action: ${intent.action}"
                )
            }
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? {
        return null
    }

    override fun onDestroy() {
        stopWatchdog()

        if (instance === this) {
            instance = null
        }

        Log.i(
            TAG,
            "Foreground watchdog destroyed"
        )

        super.onDestroy()
    }

    private fun startWatchdogLoop() {
        if (scheduler != null) {
            return
        }

        val executor =
            Executors.newSingleThreadScheduledExecutor { runnable ->
                Thread(
                    runnable,
                    "AndroidAgentPro-Watchdog"
                ).apply {
                    isDaemon = true
                }
            }

        scheduler = executor

        executor.scheduleWithFixedDelay(
            {
                performWatchdogCheck()
            },
            0L,
            WATCHDOG_INTERVAL_SECONDS,
            TimeUnit.SECONDS
        )

        Log.i(
            TAG,
            "Watchdog loop started; " +
                "interval=${WATCHDOG_INTERVAL_SECONDS}s"
        )
    }

    private fun performWatchdogCheck() {
        if (!serviceRunning.get()) {
            return
        }

        try {
            val accessibility =
                AgentAccessibilityService.getInstance()

            val accessibilityConnected =
                accessibility?.isConnected() == true

            val bridge =
                BridgeServer.getInstance(
                    applicationContext
                )

            val bridgeRunning =
                bridge.isRunning()

            Log.d(
                TAG,
                "Watchdog check: " +
                    "accessibility=$accessibilityConnected " +
                    "bridge=$bridgeRunning"
            )

            if (
                accessibilityConnected &&
                !bridgeRunning
            ) {
                restartBridgeIfAllowed()
            }
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "Watchdog check failed",
                exception
            )
        }
    }

    private fun restartBridgeIfAllowed() {
        val now =
            SystemClock.elapsedRealtime()

        if (
            now - lastBridgeRestartAt <
            BRIDGE_RESTART_COOLDOWN_MS
        ) {
            Log.w(
                TAG,
                "Bridge restart suppressed by cooldown"
            )
            return
        }

        lastBridgeRestartAt = now

        try {
            BridgeServer
                .getInstance(applicationContext)
                .start()

            Log.i(
                TAG,
                "Bridge restart requested by watchdog"
            )
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "Watchdog failed to restart bridge",
                exception
            )
        }
    }

    private fun stopWatchdog() {
        serviceRunning.set(false)

        scheduler?.let { executor ->
            try {
                executor.shutdownNow()
            } catch (exception: Exception) {
                Log.w(
                    TAG,
                    "Unable to stop watchdog scheduler",
                    exception
                )
            }
        }

        scheduler = null

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION")
                stopForeground(true)
            }
        } catch (exception: Exception) {
            Log.w(
                TAG,
                "Unable to remove watchdog notification",
                exception
            )
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return
        }

        val manager =
            getSystemService(
                NotificationManager::class.java
            )
                ?: throw IllegalStateException(
                    "NotificationManager is unavailable"
                )

        val channel = NotificationChannel(
            CHANNEL_ID,
            CHANNEL_NAME,
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = CHANNEL_DESCRIPTION
            setShowBadge(false)
        }

        manager.createNotificationChannel(channel)
    }

    private fun createNotification(): Notification {
        val launchIntent =
            Intent(this, MainActivity::class.java)

        val pendingIntent =
            PendingIntent.getActivity(
                this,
                0,
                launchIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or
                    PendingIntent.FLAG_IMMUTABLE
            )

        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(
                this,
                CHANNEL_ID
            )
                .setSmallIcon(
                    android.R.drawable.ic_dialog_info
                )
                .setContentTitle(
                    "AndroidAgentPro"
                )
                .setContentText(
                    "Bridge Watchdog يعمل"
                )
                .setContentIntent(
                    pendingIntent
                )
                .setOngoing(true)
                .setCategory(
                    Notification.CATEGORY_SERVICE
                )
                .setShowWhen(false)
                .build()
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
                .setSmallIcon(
                    android.R.drawable.ic_dialog_info
                )
                .setContentTitle(
                    "AndroidAgentPro"
                )
                .setContentText(
                    "Bridge Watchdog يعمل"
                )
                .setContentIntent(
                    pendingIntent
                )
                .setOngoing(true)
                .setCategory(
                    Notification.CATEGORY_SERVICE
                )
                .build()
        }
    }
}
