package com.ai.agentpro

import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import rikka.shizuku.Shizuku
import rikka.shizuku.ShizukuRemoteProcess
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
            val process = Shizuku.newProcess(arrayOf("sh", "-c", command), null, null)
            val output = StringBuilder()
            val error = StringBuilder()
            
            val outReader = BufferedReader(InputStreamReader(process.inputStream))
            val errReader = BufferedReader(InputStreamReader(process.errorStream))
            
            var line: String?
            while (outReader.readLine().also { line = it } != null) {
                output.append(line).append("\n")
            }
            while (errReader.readLine().also { line = it } != null) {
                error.append(line).append("\n")
            }
            
            process.waitFor()
            val exitCode = process.exitValue()
            val duration = System.currentTimeMillis() - startTime
            
            ShellResult.success(
                operationId,
                exitCode,
                output.toString().trim(),
                error.toString().trim(),
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
