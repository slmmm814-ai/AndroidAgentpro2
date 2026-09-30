package com.ai.agentpro

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class AgentAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "AndroidAgentPro.Accessibility"
        private const val GESTURE_DURATION_MS = 120L
        private const val LONG_PRESS_DURATION_MS = 600L
        private const val MAX_TREE_DEPTH = 80

        private const val MAX_TREE_NODES = 2_000

        private const val NODE_OP_TIMEOUT_MS = 4_000L

        private const val DUMP_RETRY_WINDOW_MS = 1_500L

        private const val DUMP_RETRY_PAUSE_MS = 180L

        @Volatile
        private var instance: AgentAccessibilityService? = null

        fun getInstance(): AgentAccessibilityService? = instance
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val operationCounter = AtomicLong(0L)

    @Volatile
    private var connected = false

    override fun onServiceConnected() {
        super.onServiceConnected()

        instance = this
        connected = true

        ScreenshotEngine.install(this)

        BridgeServer.getInstance(applicationContext).start()

        Log.i(TAG, "Accessibility service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) {
            return
        }

        Log.d(
            TAG,
            "event=${event.eventType}, package=${event.packageName}"
        )
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility service interrupted")
    }

    override fun onDestroy() {
        connected = false

        if (instance === this) {
            instance = null
        }

        ScreenshotEngine.clear(this)

        BridgeServer.getInstance(applicationContext).stop()

        Log.i(TAG, "Accessibility service destroyed")

        super.onDestroy()
    }

    fun isConnected(): Boolean {
        return connected && instance === this
    }

    fun dumpUi(): UiDumpResult {
        if (!isConnected()) {
            return UiDumpResult(
                success = false,
                errorCode = "ACCESSIBILITY_NOT_CONNECTED",
                errorMessage = "Accessibility service is not connected",
                root = null
            )
        }

        val deadline =
            SystemClock.uptimeMillis() + DUMP_RETRY_WINDOW_MS

        while (SystemClock.uptimeMillis() < deadline) {
            try {
                val liveRoot = rootInActiveWindow

                if (liveRoot != null) {
                    return buildDumpResult(liveRoot)
                }
            } catch (securityException: SecurityException) {
                Log.e(
                    TAG,
                    "Security error during UI dump",
                    securityException
                )

                return UiDumpResult(
                    success = false,
                    errorCode = "UI_DUMP_SECURITY_ERROR",
                    errorMessage = securityException.message
                        ?: "Security error during UI dump",
                    root = null
                )
            } catch (exception: Exception) {
                Log.e(TAG, "UI dump failed", exception)

                return UiDumpResult(
                    success = false,
                    errorCode = "UI_DUMP_FAILED",
                    errorMessage = exception.message
                        ?: "Unexpected UI dump failure",
                    root = null
                )
            }

            SystemClock.sleep(DUMP_RETRY_PAUSE_MS)
        }

        try {
            val fallbackRoot = findVisibleWindowRoot()

            if (fallbackRoot != null) {
                Log.w(TAG, "UI dump fell back to a visible window root")
                return buildDumpResult(fallbackRoot)
            }
        } catch (exception: Exception) {
            Log.e(TAG, "UI dump fallback failed", exception)
        }

        return UiDumpResult(
            success = false,
            errorCode = "UI_ROOT_UNAVAILABLE",
            errorMessage = "Active window UI root is unavailable",
            root = null
        )
    }

    private fun buildDumpResult(root: AccessibilityNodeInfo): UiDumpResult {
        val budget = NodeBudget(MAX_TREE_NODES)
        val rootNode = nodeToData(root, 0, 0, budget)
        return UiDumpResult(
            success = true,
            errorCode = null,
            errorMessage = null,
            root = rootNode,
            truncated = budget.exhausted
        )
    }

    private fun findVisibleWindowRoot(): AccessibilityNodeInfo? {
        val allWindows = try {
            windows
        } catch (exception: Exception) {
            Log.w(TAG, "Unable to list accessibility windows", exception)
            emptyList<AccessibilityWindowInfo>()
        }

        val ordered = allWindows.sortedWith(
            compareByDescending<AccessibilityWindowInfo> {
                if (it.isActive) 1 else 0
            }.thenByDescending {
                if (it.isFocused) 1 else 0
            }
        )

        for (window in ordered) {
            if (window.type ==
                AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
            ) {
                continue
            }

            val root = try {
                window.root
            } catch (exception: Exception) {
                null
            } ?: continue

            if (root.isVisibleToUser) {
                return root
            }
        }

        return null
    }

    fun tap(
        x: Float,
        y: Float,
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        if (!x.isFinite() || !y.isFinite()) {
            callback(
                GestureResult(
                    false,
                    "INVALID_COORDINATES",
                    "Coordinates must be finite",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        try {
            val path = Path().apply {
                moveTo(x, y)
            }

            val gesture = GestureDescription.Builder()
                .addStroke(
                    GestureDescription.StrokeDescription(
                        path,
                        0L,
                        GESTURE_DURATION_MS
                    )
                )
                .build()

            val dispatched = dispatchGesture(
                gesture,
                object : GestureResultCallback() {

                    override fun onCompleted(
                        completedGesture: GestureDescription?
                    ) {
                        Log.i(
                            TAG,
                            "tap completed id=$operationId x=$x y=$y"
                        )

                        callback(
                            GestureResult(
                                true,
                                null,
                                null,
                                operationId
                            )
                        )
                    }

                    override fun onCancelled(
                        cancelledGesture: GestureDescription?
                    ) {
                        Log.w(
                            TAG,
                            "tap cancelled id=$operationId"
                        )

                        callback(
                            GestureResult(
                                false,
                                "GESTURE_CANCELLED",
                                "Android cancelled the gesture",
                                operationId
                            )
                        )
                    }
                },
                mainHandler
            )

            if (!dispatched) {
                Log.w(
                    TAG,
                    "gesture dispatch rejected id=$operationId"
                )

                callback(
                    GestureResult(
                        false,
                        "GESTURE_DISPATCH_REJECTED",
                        "Android rejected the gesture",
                        operationId
                    )
                )
            }
        } catch (securityException: SecurityException) {
            Log.e(TAG, "Gesture security failure", securityException)

            callback(
                GestureResult(
                    false,
                    "GESTURE_SECURITY_ERROR",
                    securityException.message
                        ?: "Gesture security failure",
                    operationId
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "Gesture failure", exception)

            callback(
                GestureResult(
                    false,
                    "GESTURE_FAILED",
                    exception.message ?: "Unexpected gesture failure",
                    operationId
                )
            )
        }
    }

    fun back(callback: (GestureResult) -> Unit) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        try {
            val performed = performGlobalAction(GLOBAL_ACTION_BACK)

            if (performed) {
                Log.i(TAG, "back dispatched id=$operationId")

                callback(
                    GestureResult(
                        true,
                        null,
                        null,
                        operationId
                    )
                )
            } else {
                callback(
                    GestureResult(
                        false,
                        "BACK_ACTION_REJECTED",
                        "Android rejected the back action",
                        operationId
                    )
                )
            }
        } catch (exception: Exception) {
            Log.e(TAG, "Back action failed", exception)

            callback(
                GestureResult(
                    false,
                    "BACK_ACTION_FAILED",
                    exception.message ?: "Unexpected back action failure",
                    operationId
                )
            )
        }
    }

    fun swipe(
        x1: Float,
        y1: Float,
        x2: Float,
        y2: Float,
        durationMs: Long,
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        if (!x1.isFinite() || !y1.isFinite() ||
            !x2.isFinite() || !y2.isFinite()
        ) {
            callback(
                GestureResult(
                    false,
                    "INVALID_COORDINATES",
                    "Coordinates must be finite",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        try {
            val path = Path().apply {
                moveTo(x1, y1)
                lineTo(x2, y2)
            }

            val strokeDuration = if (durationMs > 0L) durationMs
                else GESTURE_DURATION_MS

            val gesture = GestureDescription.Builder()
                .addStroke(
                    GestureDescription.StrokeDescription(
                        path,
                        0L,
                        strokeDuration
                    )
                )
                .build()

            val dispatched = dispatchGesture(
                gesture,
                object : GestureResultCallback() {

                    override fun onCompleted(
                        completedGesture: GestureDescription?
                    ) {
                        Log.i(
                            TAG,
                            "swipe completed id=$operationId"
                        )
                        callback(
                            GestureResult(
                                true,
                                null,
                                null,
                                operationId
                            )
                        )
                    }

                    override fun onCancelled(
                        cancelledGesture: GestureDescription?
                    ) {
                        Log.w(
                            TAG,
                            "swipe cancelled id=$operationId"
                        )
                        callback(
                            GestureResult(
                                false,
                                "GESTURE_CANCELLED",
                                "Android cancelled the gesture",
                                operationId
                            )
                        )
                    }
                },
                mainHandler
            )

            if (!dispatched) {
                callback(
                    GestureResult(
                        false,
                        "GESTURE_DISPATCH_REJECTED",
                        "Android rejected the gesture",
                        operationId
                    )
                )
            }
        } catch (securityException: SecurityException) {
            Log.e(TAG, "Swipe security failure", securityException)
            callback(
                GestureResult(
                    false,
                    "GESTURE_SECURITY_ERROR",
                    securityException.message
                        ?: "Gesture security failure",
                    operationId
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "Swipe failure", exception)
            callback(
                GestureResult(
                    false,
                    "GESTURE_FAILED",
                    exception.message ?: "Unexpected swipe failure",
                    operationId
                )
            )
        }
    }

    fun longPress(
        x: Float,
        y: Float,
        durationMs: Long,
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        if (!x.isFinite() || !y.isFinite()) {
            callback(
                GestureResult(
                    false,
                    "INVALID_COORDINATES",
                    "Coordinates must be finite",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        try {
            val path = Path().apply {
                moveTo(x, y)
            }

            val strokeDuration = if (durationMs in 100L..10_000L)
                durationMs else LONG_PRESS_DURATION_MS

            val gesture = GestureDescription.Builder()
                .addStroke(
                    GestureDescription.StrokeDescription(
                        path,
                        0L,
                        strokeDuration
                    )
                )
                .build()

            val dispatched = dispatchGesture(
                gesture,
                object : GestureResultCallback() {

                    override fun onCompleted(
                        completedGesture: GestureDescription?
                    ) {
                        Log.i(
                            TAG,
                            "long_press completed id=$operationId x=$x y=$y"
                        )
                        callback(
                            GestureResult(
                                true,
                                null,
                                null,
                                operationId
                            )
                        )
                    }

                    override fun onCancelled(
                        cancelledGesture: GestureDescription?
                    ) {
                        Log.w(
                            TAG,
                            "long_press cancelled id=$operationId"
                        )
                        callback(
                            GestureResult(
                                false,
                                "GESTURE_CANCELLED",
                                "Android cancelled the gesture",
                                operationId
                            )
                        )
                    }
                },
                mainHandler
            )

            if (!dispatched) {
                callback(
                    GestureResult(
                        false,
                        "GESTURE_DISPATCH_REJECTED",
                        "Android rejected the gesture",
                        operationId
                    )
                )
            }
        } catch (securityException: SecurityException) {
            Log.e(TAG, "Long press security failure", securityException)
            callback(
                GestureResult(
                    false,
                    "GESTURE_SECURITY_ERROR",
                    securityException.message
                        ?: "Gesture security failure",
                    operationId
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "Long press failure", exception)
            callback(
                GestureResult(
                    false,
                    "GESTURE_FAILED",
                    exception.message ?: "Unexpected long press failure",
                    operationId
                )
            )
        }
    }

    fun inputText(
        text: String,
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        if (text.isEmpty()) {
            callback(
                GestureResult(
                    false,
                    "INPUT_EMPTY_TEXT",
                    "input_text requires a non-empty text",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        val result = runNodeOperationOnMain(
            deadlineMs = NODE_OP_TIMEOUT_MS,
            operationName = "input_text",
            onTimeout = GestureResult(
                false,
                "INPUT_TIMEOUT",
                "Input did not finish in time",
                operationId
            ),
            nodeOp = {
                val root = rootInActiveWindow
                val target = findEditableTarget(root)

                if (target == null) {
                    GestureResult(
                        false,
                        "INPUT_NO_FOCUS",
                        "No editable field available to receive text",
                        operationId
                    )
                } else {
                    ensureEditableFocused(target)

                    val args = Bundle().apply {
                        putCharSequence(
                            AccessibilityNodeInfo
                                .ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            text
                        )
                    }

                    val performed = target.performAction(
                        AccessibilityNodeInfo.ACTION_SET_TEXT,
                        args
                    )

                    if (performed) {
                        Log.i(
                            TAG,
                            "input_text completed id=$operationId " +
                                "method=set_text len=${text.length}"
                        )
                        GestureResult(
                            true,
                            null,
                            null,
                            operationId
                        )
                    } else {
                        Log.w(
                            TAG,
                            "input_text ACTION_SET_TEXT rejected " +
                                "id=$operationId, falling back to " +
                                "clipboard paste"
                        )
                        clipboardPasteInto(
                            target,
                            text,
                            operationId
                        )
                    }
                }
            }
        )

        callback(result)
    }

    fun clearText(
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        val result = runNodeOperationOnMain(
            deadlineMs = NODE_OP_TIMEOUT_MS,
            operationName = "clear_text",
            onTimeout = GestureResult(
                false,
                "CLEAR_TIMEOUT",
                "Clear did not finish in time",
                operationId
            ),
            nodeOp = {
                val root = rootInActiveWindow
                val target = findEditableTarget(root)

                if (target == null) {
                    GestureResult(
                        false,
                        "CLEAR_NO_FOCUS",
                        "No editable field available to clear",
                        operationId
                    )
                } else {
                    ensureEditableFocused(target)

                    val args = Bundle().apply {
                        putCharSequence(
                            AccessibilityNodeInfo
                                .ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            ""
                        )
                    }

                    val performed = target.performAction(
                        AccessibilityNodeInfo.ACTION_SET_TEXT,
                        args
                    )

                    if (performed) {
                        Log.i(
                            TAG,
                            "clear_text completed id=$operationId"
                        )
                        GestureResult(
                            true,
                            null,
                            null,
                            operationId
                        )
                    } else {
                        GestureResult(
                            false,
                            "CLEAR_SET_TEXT_REJECTED",
                            "Field rejected ACTION_SET_TEXT",
                            operationId
                        )
                    }
                }
            }
        )

        callback(result)
    }

    fun eraseText(
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        val result = runNodeOperationOnMain(
            deadlineMs = NODE_OP_TIMEOUT_MS,
            operationName = "erase_text",
            onTimeout = GestureResult(
                false,
                "ERASE_TIMEOUT",
                "Erase did not finish in time",
                operationId
            ),
            nodeOp = {
                val root = rootInActiveWindow
                val target = findEditableTarget(root)

                if (target == null) {
                    GestureResult(
                        false,
                        "ERASE_NO_FOCUS",
                        "No editable field available to erase",
                        operationId
                    )
                } else {
                    ensureEditableFocused(target)

                    var erased = false

                    val setSelectionAttempted = target.performAction(
                        AccessibilityNodeInfo.ACTION_SET_SELECTION,
                        Bundle().apply {
                            putInt(
                                AccessibilityNodeInfo
                                    .ACTION_ARGUMENT_SELECTION_START_INT,
                                0
                            )
                            putInt(
                                AccessibilityNodeInfo
                                    .ACTION_ARGUMENT_SELECTION_END_INT,
                                Integer.MAX_VALUE
                            )
                        }
                    )

                    if (setSelectionAttempted) {
                        for (attempt in 0 until 3) {
                            val cutPerformed = target.performAction(
                                AccessibilityNodeInfo.ACTION_CUT
                            )
                            if (cutPerformed) {
                                erased = true
                                break
                            }
                            val text =
                                target.text?.toString() ?: ""
                            if (text.isEmpty()) {
                                erased = true
                                break
                            }
                            SystemClock.sleep(80L)
                        }
                    }

                    if (!erased) {
                        val args = Bundle().apply {
                            putCharSequence(
                                AccessibilityNodeInfo
                                    .ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                                ""
                            )
                        }
                        erased = target.performAction(
                            AccessibilityNodeInfo.ACTION_SET_TEXT,
                            args
                        )
                    }

                    if (erased) {
                        Log.i(
                            TAG,
                            "erase_text completed id=$operationId"
                        )
                        GestureResult(
                            true,
                            null,
                            null,
                            operationId
                        )
                    } else {
                        GestureResult(
                            false,
                            "ERASE_REJECTED",
                            "Field rejected erase actions",
                            operationId
                        )
                    }
                }
            }
        )

        callback(result)
    }

    fun listWindows(): WindowListResult {
        if (!isConnected()) {
            return WindowListResult(
                success = false,
                errorCode = "ACCESSIBILITY_NOT_CONNECTED",
                errorMessage = "Accessibility service is not connected",
                windows = emptyList()
            )
        }

        return try {
            val allWindows = windows

            if (allWindows.isNullOrEmpty()) {
                return WindowListResult(
                    success = false,
                    errorCode = "NO_WINDOWS",
                    errorMessage =
                        "No interactive windows are currently visible",
                    windows = emptyList()
                )
            }

            val entries = allWindows
                .filter {
                    it.type !=
                        AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
                }
                .map { window ->
                    val root = try {
                        window.root
                    } catch (exception: Exception) {
                        Log.w(
                            TAG,
                            "Unable to read a window root",
                            exception
                        )

                        null
                    }

                    val bounds = Rect()
                    window.getBoundsInScreen(bounds)

                    WindowEntry(
                        id = window.id,
                        packageName = root?.packageName?.toString(),
                        title = window.title?.toString(),
                        active = window.isActive,
                        focused = window.isFocused,
                        windowType = window.type,
                        boundsLeft = bounds.left,
                        boundsTop = bounds.top,
                        boundsRight = bounds.right,
                        boundsBottom = bounds.bottom,
                        rootChildCount = root?.childCount ?: 0
                    )
                }

            WindowListResult(
                success = true,
                errorCode = null,
                errorMessage = null,
                windows = entries
            )
        } catch (exception: Exception) {
            Log.e(TAG, "list_windows failed", exception)

            WindowListResult(
                success = false,
                errorCode = "LIST_WINDOWS_FAILED",
                errorMessage =
                    exception.message ?: "Unable to list windows",
                windows = emptyList()
            )
        }
    }

    fun dumpUiForPackage(packageName: String): UiDumpResult {
        if (!isConnected()) {
            return UiDumpResult(
                success = false,
                errorCode = "ACCESSIBILITY_NOT_CONNECTED",
                errorMessage = "Accessibility service is not connected",
                root = null
            )
        }

        if (packageName.isBlank()) {
            return UiDumpResult(
                success = false,
                errorCode = "EMPTY_PACKAGE_NAME",
                errorMessage = "ui_dump requires a non-empty package name",
                root = null
            )
        }

        val deadline =
            SystemClock.uptimeMillis() + DUMP_RETRY_WINDOW_MS

        while (SystemClock.uptimeMillis() < deadline) {
            try {
                val packageRoot =
                    findWindowRootForPackage(packageName)

                if (packageRoot != null) {
                    return buildDumpResult(packageRoot)
                }
            } catch (securityException: SecurityException) {
                Log.e(
                    TAG,
                    "Security error during package UI dump",
                    securityException
                )

                return UiDumpResult(
                    success = false,
                    errorCode = "UI_DUMP_SECURITY_ERROR",
                    errorMessage = securityException.message
                        ?: "Security error during UI dump",
                    root = null
                )
            } catch (exception: Exception) {
                Log.e(TAG, "package UI dump failed", exception)

                return UiDumpResult(
                    success = false,
                    errorCode = "UI_DUMP_FAILED",
                    errorMessage = exception.message
                        ?: "Unexpected package UI dump failure",
                    root = null
                )
            }

            SystemClock.sleep(DUMP_RETRY_PAUSE_MS)
        }

        return UiDumpResult(
            success = false,
            errorCode = "PACKAGE_NOT_FOUND",
            errorMessage =
                "No visible window for package $packageName",
            root = null
        )
    }

    fun nodeAction(
        action: String,
        targetPackage: String?,
        resourceId: String?,
        text: String?,
        contentDescription: String?,
        className: String?,
        matchIndex: Int,
        argumentText: String?,
        nodePath: IntArray?,
        callback: (NodeActionResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                NodeActionResult(
                    success = false,
                    errorCode = "ACCESSIBILITY_NOT_CONNECTED",
                    errorMessage =
                        "Accessibility service is not connected",
                    operationId = null,
                    matched = null
                )
            )
            return
        }

        val normalizedAction = action.trim().lowercase()

        val supported = setOf(
            "click",
            "long_click",
            "scroll_forward",
            "scroll_backward",
            "set_text",
            "focus",
            "clear_focus",
            "select"
        )

        if (normalizedAction.isEmpty() ||
            normalizedAction !in supported
        ) {
            callback(
                NodeActionResult(
                    success = false,
                    errorCode = "UNSUPPORTED_NODE_ACTION",
                    errorMessage =
                        "Unsupported node action: $action",
                    operationId = null,
                    matched = null
                )
            )
            return
        }

        if (normalizedAction == "set_text" &&
            argumentText.isNullOrEmpty()
        ) {
            callback(
                NodeActionResult(
                    success = false,
                    errorCode = "MISSING_NODE_TEXT",
                    errorMessage =
                        "set_text requires a non-empty text argument",
                    operationId = null,
                    matched = null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        val result = runNodeActionResultOnMain(
            deadlineMs = NODE_OP_TIMEOUT_MS,
            operationName = "node_action",
            onTimeout = NodeActionResult(
                success = false,
                errorCode = "NODE_ACTION_TIMEOUT",
                errorMessage =
                    "Node action did not finish in time",
                operationId = operationId,
                matched = null
            ),
            nodeOp = {
                val root = if (targetPackage.isNullOrBlank()) {
                    rootInActiveWindow
                } else {
                    findWindowRootForPackage(targetPackage)
                }

                if (root == null) {
                    NodeActionResult(
                        success = false,
                        errorCode = "NODE_WINDOW_UNAVAILABLE",
                        errorMessage = if (targetPackage.isNullOrBlank()) {
                            "No active accessibility window"
                        } else {
                            "No visible window for package $targetPackage"
                        },
                        operationId = operationId,
                        matched = null
                    )
                } else {
                    performNodeAction(
                        operationId = operationId,
                        root = root,
                        action = normalizedAction,
                        resourceId = resourceId,
                        text = text,
                        contentDescription = contentDescription,
                        className = className,
                        matchIndex = matchIndex,
                        argumentText = argumentText,
                        nodePath = nodePath
                    )
                }
            }
        )

        callback(result)
    }

    private fun findWindowRootForPackage(
        packageName: String
    ): AccessibilityNodeInfo? {
        val allWindows = try {
            windows
        } catch (exception: Exception) {
            Log.w(
                TAG,
                "Unable to list windows for package lookup",
                exception
            )

            return null
        }

        for (window in allWindows) {
            if (window.type ==
                AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
            ) {
                continue
            }

            val root = try {
                window.root
            } catch (exception: Exception) {
                null
            } ?: continue

            if (root.isVisibleToUser &&
                root.packageName?.toString() == packageName
            ) {
                return root
            }
        }

        return null
    }

    private fun resolveNodeByPath(
        root: AccessibilityNodeInfo,
        path: IntArray
    ): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo = root

        for (step in path) {
            if (step < 0 || step >= current.childCount) {
                return null
            }

            current = current.getChild(step) ?: return null
        }

        return current
    }

    private fun performNodeAction(
        operationId: Long,
        root: AccessibilityNodeInfo,
        action: String,
        resourceId: String?,
        text: String?,
        contentDescription: String?,
        className: String?,
        matchIndex: Int,
        argumentText: String?,
        nodePath: IntArray?
    ): NodeActionResult {
        val target: AccessibilityNodeInfo

        if (nodePath != null && nodePath.isNotEmpty()) {
            target = resolveNodeByPath(root, nodePath)
                ?: return NodeActionResult(
                    success = false,
                    errorCode = "NODE_PATH_INVALID",
                    errorMessage =
                        "The node path does not resolve in this window",
                    operationId = operationId,
                    matched = null
                )
        } else {
            val matches = findMatchingNodes(
                root = root,
                resourceId = resourceId,
                text = text,
                contentDescription = contentDescription,
                className = className
            )

            if (matches.isEmpty()) {
                return NodeActionResult(
                    success = false,
                    errorCode = "NODE_NOT_FOUND",
                    errorMessage =
                        "No node matched the given selector in this window",
                    operationId = operationId,
                    matched = null
                )
            }

            val safeIndex = if (matchIndex < 0) {
                0
            } else if (matchIndex >= matches.size) {
                matches.size - 1
            } else {
                matchIndex
            }

            target = matches[safeIndex]
        }

        val bounds = Rect()
        target.getBoundsInScreen(bounds)

        val summary = MatchedNode(
            packageName = target.packageName?.toString(),
            resourceId = target.viewIdResourceName,
            text = target.text?.toString(),
            contentDescription = target.contentDescription?.toString(),
            className = target.className?.toString(),
            matchIndex = if (nodePath != null) -1 else matchIndex,
            matchCount = if (nodePath != null) 1 else 1,
            boundsLeft = bounds.left,
            boundsTop = bounds.top,
            boundsRight = bounds.right,
            boundsBottom = bounds.bottom
        )

        val performed = when (action) {
            "click" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_CLICK
                )
            "long_click" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_LONG_CLICK
                )
            "scroll_forward" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                )
            "scroll_backward" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                )
            "focus" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_FOCUS
                )
            "clear_focus" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_CLEAR_FOCUS
                )
            "select" ->
                target.performAction(
                    AccessibilityNodeInfo.ACTION_SELECT
                )
            else -> {
                val args = Bundle().apply {
                    putCharSequence(
                        AccessibilityNodeInfo
                            .ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                        argumentText
                    )
                }

                target.performAction(
                    AccessibilityNodeInfo.ACTION_SET_TEXT,
                    args
                )
            }
        }

        return if (performed) {
            Log.i(
                TAG,
                "node_action completed id=$operationId " +
                    "action=$action package=${summary.packageName}"
            )

            NodeActionResult(
                success = true,
                errorCode = null,
                errorMessage = null,
                operationId = operationId,
                matched = summary
            )
        } else {
            Log.w(
                TAG,
                "node_action rejected id=$operationId action=$action"
            )

            NodeActionResult(
                success = false,
                errorCode = "NODE_ACTION_REJECTED",
                errorMessage =
                    "The node rejected the requested action $action",
                operationId = operationId,
                matched = summary
            )
        }
    }

    private fun findMatchingNodes(
        root: AccessibilityNodeInfo?,
        resourceId: String?,
        text: String?,
        contentDescription: String?,
        className: String?
    ): List<AccessibilityNodeInfo> {
        if (root == null) {
            return emptyList()
        }

        val matches = ArrayList<AccessibilityNodeInfo>()

        fun visit(node: AccessibilityNodeInfo) {
            if (matchesNodeSelector(
                    node = node,
                    resourceId = resourceId,
                    text = text,
                    contentDescription = contentDescription,
                    className = className
                )
            ) {
                matches.add(node)
            }

            for (index in 0 until node.childCount) {
                val child = node.getChild(index) ?: continue
                visit(child)
            }
        }

        visit(root)
        return matches
    }

    private fun matchesNodeSelector(
        node: AccessibilityNodeInfo,
        resourceId: String?,
        text: String?,
        contentDescription: String?,
        className: String?
    ): Boolean {
        if (resourceId != null &&
            node.viewIdResourceName != resourceId
        ) {
            return false
        }

        if (text != null && node.text?.toString() != text) {
            return false
        }

        if (contentDescription != null &&
            node.contentDescription?.toString() != contentDescription
        ) {
            return false
        }

        if (className != null &&
            node.className?.toString() != className
        ) {
            return false
        }

        return true
    }

    private fun runNodeActionResultOnMain(
        deadlineMs: Long,
        operationName: String,
        onTimeout: NodeActionResult,
        nodeOp: () -> NodeActionResult
    ): NodeActionResult {
        val gate = java.util.concurrent.CountDownLatch(1)
        val cell =
            java.util.concurrent.atomic.AtomicReference<NodeActionResult?>(null)
        val failure =
            java.util.concurrent.atomic.AtomicReference<Throwable?>(null)

        mainHandler.post {
            try {
                cell.set(nodeOp())
            } catch (securityException: SecurityException) {
                failure.set(securityException)
            } catch (exception: Exception) {
                failure.set(exception)
            } finally {
                gate.countDown()
            }
        }

        if (!gate.await(deadlineMs, TimeUnit.MILLISECONDS)) {
            Log.w(
                TAG,
                "$operationName timed out waiting for the main thread"
            )
            return onTimeout
        }

        failure.get()?.let {
            Log.e(TAG, "$operationName failed on the main thread", it)

            return NodeActionResult(
                success = false,
                errorCode =
                    "${operationName.uppercase()}_MAIN_THREAD_ERROR"
                        .replace(' ', '_'),
                errorMessage = it.message ?: "Node operation failed",
                operationId = null,
                matched = null
            )
        }

        return cell.get() ?: onTimeout
    }

    fun windowInfo(): WindowInfoResult {
        if (!isConnected()) {
            return WindowInfoResult(
                packageName = null,
                activityName = null,
                windowTitle = null
            )
        }

        return try {
            val root = rootInActiveWindow
            val packageName = root?.packageName?.toString()

            var windowTitle: String? = null
            val windows = windows
            if (!windows.isNullOrEmpty()) {
                val appWindow = windows.firstOrNull {
                    it.type == AccessibilityWindowInfo.TYPE_APPLICATION
                } ?: windows.firstOrNull()
                windowTitle = appWindow?.title?.toString()
            }

            WindowInfoResult(
                packageName = packageName,
                activityName = null,
                windowTitle = windowTitle
            )
        } catch (exception: Exception) {
            Log.e(TAG, "window_info failed", exception)
            WindowInfoResult(
                packageName = null,
                activityName = null,
                windowTitle = null
            )
        }
    }

    fun keyEvent(
        keycode: String,
        callback: (GestureResult) -> Unit
    ) {
        if (!isConnected()) {
            callback(
                GestureResult(
                    false,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected",
                    null
                )
            )
            return
        }

        val operationId = operationCounter.incrementAndGet()

        val action = when (keycode.lowercase()) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents", "overview" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
            "power_dialog" -> GLOBAL_ACTION_POWER_DIALOG
            else -> {
                callback(
                    GestureResult(
                        false,
                        "UNSUPPORTED_KEYCODE",
                        "Unsupported keycode: $keycode. Supported: " +
                            "back, home, recents, notifications, " +
                            "quick_settings, power_dialog",
                        operationId
                    )
                )
                return
            }
        }

        try {
            val performed = performGlobalAction(action)

            if (performed) {
                Log.i(
                    TAG,
                    "key_event $keycode dispatched id=$operationId"
                )
                callback(
                    GestureResult(
                        true,
                        null,
                        null,
                        operationId
                    )
                )
            } else {
                callback(
                    GestureResult(
                        false,
                        "KEY_EVENT_REJECTED",
                        "Android rejected key event $keycode",
                        operationId
                    )
                )
            }
        } catch (exception: Exception) {
            Log.e(TAG, "key_event failure", exception)
            callback(
                GestureResult(
                    false,
                    "KEY_EVENT_FAILED",
                    exception.message
                        ?: "Unexpected key event failure",
                    operationId
                )
            )
        }
    }

    private fun findEditableTarget(
        root: AccessibilityNodeInfo?
    ): AccessibilityNodeInfo? {
        if (root == null) {
            return null
        }

        // Avoid FOCUS_INPUT lookups off the main thread: on some builds
        // they can block until an input window materializes. Prefer the
        // accessibility focus, then any focused editable, then the first
        // editable field available.
        val accessibleFocused = root.findFocus(
            AccessibilityNodeInfo.FOCUS_ACCESSIBILITY
        )

        if (accessibleFocused != null && isEditable(accessibleFocused)) {
            return accessibleFocused
        }

        val focusedEditable = findEditableFocused(root)

        if (focusedEditable != null) {
            return focusedEditable
        }

        return findFirstEditable(root)
    }

    private fun findFirstEditable(
        node: AccessibilityNodeInfo
    ): AccessibilityNodeInfo? {
        if (isEditable(node)) {
            return node
        }

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val found = findFirstEditable(child)
            if (found != null) {
                return found
            }
        }

        return null
    }

    private fun ensureEditableFocused(
        target: AccessibilityNodeInfo
    ) {
        if (!target.isFocused) {
            target.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            SystemClock.sleep(120L)
        }
    }

    /**
     * Fallback input path: write the text to the clipboard, open the
     * paste (long-press) menu on the field, and tap the paste action.
     * This mirrors the input strategy of the earlier builds that typed
     * reliably on devices where ACTION_SET_TEXT is rejected.
     *
     * Must be invoked on the main thread.
     */
    private fun clipboardPasteInto(
        target: AccessibilityNodeInfo,
        text: String,
        operationId: Long
    ): GestureResult {
        try {
            val bounds = Rect()
            target.getBoundsInScreen(bounds)

            val cx = bounds.exactCenterX()
            val cy = bounds.exactCenterY()

            val clipboard = getSystemService(
                ClipboardManager::class.java
            )

            clipboard.setPrimaryClip(
                ClipData.newPlainText("agentpro_input", text)
            )
            Log.i(TAG, "clipboard written for paste id=$operationId")

            longPress(cx, cy, LONG_PRESS_DURATION_MS) {
                Log.i(TAG, "paste long-press dispatched cfid=$operationId")
            }

            SystemClock.sleep(1_000L)

            val root = rootInActiveWindow
            val pasteNode = findPasteNode(root)

            if (pasteNode == null) {
                Log.w(TAG, "paste menu item not found id=$operationId")
                return GestureResult(
                    false,
                    "INPUT_PASTE_MENU_MISSING",
                    "Clipboard pasted but no paste action found",
                    operationId
                )
            }

            val clicked = pasteNode.performAction(
                AccessibilityNodeInfo.ACTION_CLICK
            )

            SystemClock.sleep(500L)

            if (clicked && fieldShowsText(text)) {
                Log.i(
                    TAG,
                    "input_text completed id=$operationId " +
                        "method=clipboard_paste"
                )
                return GestureResult(
                    true,
                    null,
                    "method=clipboard_paste",
                    operationId
                )
            }
            return GestureResult(
                false,
                "INPUT_PASTE_FAILED",
                "Paste action performed but text was not applied",
                operationId
            )
        } catch (securityException: SecurityException) {
            Log.e(TAG, "clipboard paste security failure", securityException)
            return GestureResult(
                false,
                "INPUT_PASTE_SECURITY_ERROR",
                securityException.message ?: "Security failure",
                operationId
            )
        } catch (exception: Exception) {
            Log.e(TAG, "clipboard paste failure", exception)
            return GestureResult(
                false,
                "INPUT_PASTE_FAILED",
                exception.message ?: "Unexpected paste failure",
                operationId
            )
        }
    }

    private fun findPasteNode(
        root: AccessibilityNodeInfo?
    ): AccessibilityNodeInfo? {
        if (root == null) {
            return null
        }

        val candidates = ArrayList<AccessibilityNodeInfo>()

        fun collect(node: AccessibilityNodeInfo) {
            val text = node.text?.toString().orEmpty()
            val description =
                node.contentDescription?.toString().orEmpty()
            val resourceId =
                node.viewIdResourceName.orEmpty()

            val label = "$text $description".lowercase()
            val hasArabicPasteLabel =
                text.contains("لصق") || description.contains("لصق")
            val hasEnglishPasteLabel =
                label.contains("paste") || label.contains("clipboard")

            if (hasArabicPasteLabel ||
                hasEnglishPasteLabel ||
                resourceId.contains("paste", ignoreCase = true) ||
                resourceId.contains("menu", ignoreCase = true)
            ) {
                candidates.add(node)
            }

            for (index in 0 until node.childCount) {
                val child = node.getChild(index) ?: continue
                collect(child)
            }
        }

        collect(root)

        return candidates.maxByOrNull {
            it.isClickable.toInt()
        } ?: candidates.firstOrNull()
    }

    private fun fieldShowsText(expected: String): Boolean {
        val root = rootInActiveWindow ?: return false

        var lastEditableText: String? = null

        fun scan(node: AccessibilityNodeInfo) {
            if (node.isEditable ||
                node.className?.toString()
                    ?.contains("EditText", ignoreCase = true) == true
            ) {
                lastEditableText = node.text?.toString()
            }
            for (index in 0 until node.childCount) {
                val child = node.getChild(index) ?: continue
                scan(child)
            }
        }

        scan(root)

        val current = lastEditableText.orEmpty()
        return expected.isNotEmpty() && current.contains(expected)
    }

    private fun Boolean.toInt(): Int = if (this) 1 else 0

    private fun runNodeOperationOnMain(
        deadlineMs: Long,
        operationName: String,
        onTimeout: GestureResult,
        nodeOp: () -> GestureResult
    ): GestureResult {
        val gate = java.util.concurrent.CountDownLatch(1)
        val cell =
            java.util.concurrent.atomic.AtomicReference<GestureResult?>(
                null
            )
        val failure =
            java.util.concurrent.atomic.AtomicReference<Throwable?>(
                null
            )

        mainHandler.post {
            try {
                cell.set(nodeOp())
            } catch (securityException: SecurityException) {
                failure.set(securityException)
            } catch (exception: Exception) {
                failure.set(exception)
            } finally {
                gate.countDown()
            }
        }

        if (!gate.await(deadlineMs, TimeUnit.MILLISECONDS)) {
            Log.w(
                TAG,
                "$operationName timed out waiting for the main thread"
            )
            return onTimeout
        }

        failure.get()?.let {
            Log.e(TAG, "$operationName failed on the main thread", it)
            return GestureResult(
                false,
                "${operationName.uppercase()}_MAIN_THREAD_ERROR"
                    .replace(' ', '_'),
                it.message ?: "Node operation failed",
                null
            )
        }

        return cell.get() ?: onTimeout
    }

    private fun findEditableFocused(
        node: AccessibilityNodeInfo
    ): AccessibilityNodeInfo? {
        if (node.isFocused && isEditable(node)) {
            return node
        }

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            val found = findEditableFocused(child)
            if (found != null) {
                return found
            }
        }

        return null
    }

    private fun isEditable(node: AccessibilityNodeInfo): Boolean {
        val className = node.className?.toString()
        return node.isEditable ||
            className == "android.widget.EditText" ||
            className == "android.widget.AutoCompleteTextView" ||
            className == "android.widget.MultiAutoCompleteTextView"
    }

    private fun nodeToData(
        node: AccessibilityNodeInfo,
        depth: Int,
        index: Int,
        budget: NodeBudget
    ): UiNodeData {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)

        val children = ArrayList<UiNodeData>()

        val hasBudget = budget.consume()

        if (hasBudget && depth < MAX_TREE_DEPTH) {
            for (childIndex in 0 until node.childCount) {
                if (budget.exhausted) {
                    break
                }
                val child = node.getChild(childIndex) ?: continue

                children.add(
                    nodeToData(
                        child,
                        depth + 1,
                        childIndex,
                        budget
                    )
                )
            }
        }

        return UiNodeData(
            index = index,
            depth = depth,
            className = node.className?.toString(),
            packageName = node.packageName?.toString(),
            viewIdResourceName = node.viewIdResourceName,
            text = node.text?.toString(),
            contentDescription = node.contentDescription?.toString(),
            clickable = node.isClickable,
            enabled = node.isEnabled,
            focusable = node.isFocusable,
            focused = node.isFocused,
            scrollable = node.isScrollable,
            selected = node.isSelected,
            visibleToUser = node.isVisibleToUser,
            boundsLeft = bounds.left,
            boundsTop = bounds.top,
            boundsRight = bounds.right,
            boundsBottom = bounds.bottom,
            checked = node.isChecked,
            editable = node.isEditable,
            children = children
        )
    }

    data class GestureResult(
        val success: Boolean,
        val errorCode: String?,
        val errorMessage: String?,
        val operationId: Long?
    )

    data class UiDumpResult(
        val success: Boolean,
        val errorCode: String?,
        val errorMessage: String?,
        val root: UiNodeData?,
        val truncated: Boolean = false
    )

    private class NodeBudget(
        private val maxNodes: Int
    ) {
        private var nodesUsed = 0

        val exhausted: Boolean
            get() = nodesUsed >= maxNodes

        fun consume(): Boolean {
            if (exhausted) {
                return false
            }
            nodesUsed += 1
            return true
        }
    }

    data class UiNodeData(
        val index: Int,
        val depth: Int,
        val className: String?,
        val packageName: String?,
        val viewIdResourceName: String?,
        val text: String?,
        val contentDescription: String?,
        val clickable: Boolean,
        val enabled: Boolean,
        val focusable: Boolean,
        val focused: Boolean,
        val scrollable: Boolean,
        val selected: Boolean,
        val visibleToUser: Boolean,
        val boundsLeft: Int,
        val boundsTop: Int,
        val boundsRight: Int,
        val boundsBottom: Int,
        val checked: Boolean = false,
        val editable: Boolean = false,
        val children: List<UiNodeData>
    )

    data class WindowInfoResult(
        val packageName: String?,
        val activityName: String?,
        val windowTitle: String?
    )

    data class WindowEntry(
        val id: Int,
        val packageName: String?,
        val title: String?,
        val active: Boolean,
        val focused: Boolean,
        val windowType: Int,
        val boundsLeft: Int,
        val boundsTop: Int,
        val boundsRight: Int,
        val boundsBottom: Int,
        val rootChildCount: Int
    )

    data class WindowListResult(
        val success: Boolean,
        val errorCode: String?,
        val errorMessage: String?,
        val windows: List<WindowEntry>
    )

    data class MatchedNode(
        val packageName: String?,
        val resourceId: String?,
        val text: String?,
        val contentDescription: String?,
        val className: String?,
        val matchIndex: Int,
        val matchCount: Int,
        val boundsLeft: Int,
        val boundsTop: Int,
        val boundsRight: Int,
        val boundsBottom: Int
    )

    data class NodeActionResult(
        val success: Boolean,
        val errorCode: String?,
        val errorMessage: String?,
        val operationId: Long?,
        val matched: MatchedNode?
    )
}
