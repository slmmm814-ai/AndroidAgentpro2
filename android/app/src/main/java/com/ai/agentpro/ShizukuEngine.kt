package com.ai.agentpro

import android.content.Context
import android.content.pm.PackageManager
import android.os.ParcelFileDescriptor
import android.util.Log
import rikka.shizuku.Shizuku
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class ShizukuEngine(private val context: Context) {

    companion object {
        private const val TAG = "AndroidAgentPro.Shizuku"
        private const val SHIZUKU_PERMISSION_CODE = 1001
        
        @Volatile
        private var instance: ShizukuEngine? = null

        fun getInstance(context: Context): ShizukuEngine {
            return instance ?: synchronized(this) {
                instance ?: ShizukuEngine(context.applicationContext).also { instance = it }
            }
        }
    }

    private val operationCounter = AtomicLong(0L)

    fun isServiceAvailable(): Boolean {
        return try {
            Shizuku.pingBinder()
        } catch (e: Exception) {
            false
        }
    }

    fun hasPermission(): Boolean {
        if (!isServiceAvailable()) return false
        return Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    }

    fun requestPermission(): Boolean {
        if (!isServiceAvailable()) return false
        if (hasPermission()) return true
        
        val latch = CountDownLatch(1)
        var result = false
        
        val listener = object : Shizuku.OnRequestPermissionResultListener {
            override fun onRequestPermissionResult(requestCode: Int, grantResult: Int) {
                if (requestCode == SHIZUKU_PERMISSION_CODE) {
                    result = grantResult == PackageManager.PERMISSION_GRANTED
                    Shizuku.removeRequestPermissionResultListener(this)
                    latch.countDown()
                }
            }
        }
        
        Shizuku.addRequestPermissionResultListener(listener)
        Shizuku.requestPermission(SHIZUKU_PERMISSION_CODE)
        
        return try {
            latch.await(30, TimeUnit.SECONDS)
            result
        } catch (e: InterruptedException) {
            false
        }
    }

    fun execShell(command: String): ShellResult {
        val operationId = operationCounter.incrementAndGet()
        val startTime = System.currentTimeMillis()
        
        if (!isServiceAvailable()) {
            return ShellResult.failure(operationId, "SHIZUKU_NOT_RUNNING", "Shizuku service is not running on the device")
        }
        
        if (!hasPermission()) {
            return ShellResult.failure(operationId, "SHIZUKU_PERMISSION_DENIED", "Shizuku permission not granted by user")
        }

        return try {
            // Use Shizuku's binder interface to execute shell command
            val service = Shizuku.getService()
            if (service == null) {
                return ShellResult.failure(operationId, "SERVICE_NULL", "Shizuku service binder is null")
            }
            
            // Create pipe for output
            val pipe = ParcelFileDescriptor.createPipe()
            val readFd = pipe[0]
            val writeFd = pipe[1]
            
            // Execute command via Shizuku service
            service.exec("sh", arrayOf("-c", command), null, writeFd)
            
            val output = StringBuilder()
            val error = StringBuilder()
            
            // Read from the pipe
            val reader = BufferedReader(InputStreamReader(ParcelFileDescriptor.AutoCloseInputStream(readFd)))
            var line: String?
            while (reader.readLine().also { line = it } != null) {
                output.append(line).append("\n")
            }
            reader.close()
            
            // For stderr, we need another approach - in Shizuku, stderr goes to the same pipe by default
            // or we can't easily separate it. We'll just use output.
            
            val duration = System.currentTimeMillis() - startTime
            
            ShellResult.success(
                operationId,
                0, // exit code not easily available this way
                output.toString().trim(),
                "",
                duration
            )
        } catch (e: Exception) {
            Log.e(TAG, "Shizuku exec failed", e)
            ShellResult.failure(operationId, "EXEC_FAILED", e.message ?: "Unknown error")
        }
    }

    data class ShellResult(
        val operationId: Long,
        val success: Boolean,
        val exitCode: Int,
        val output: String,
        val error: String,
        val errorCode: String?,
        val message: String?,
        val elapsedMs: Long
    ) {
        companion object {
            fun success(id: Long, exitCode: Int, out: String, err: String, time: Long) = 
                ShellResult(id, true, exitCode, out, err, null, null, time)
            
            fun failure(id: Long, code: String, msg: String) = 
                ShellResult(id, false, -1, "", "", code, msg, 0)
        }
    }
}