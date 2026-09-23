package com.ai.agentpro

import android.content.Context
import android.util.Base64
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import java.util.UUID

object BridgeProtocol {

    const val PROTOCOL = "ultimate"
    const val VERSION = "1.0"
    const val SERVER_HOST = "127.0.0.1"
    const val SERVER_PORT = 8070

    private const val TAG = "AndroidAgentPro.Protocol"
    private const val PREFS_NAME = "android_agent_pro_bridge"
    private const val TOKEN_KEY = "bridge_token"

    private val secureRandom = SecureRandom()

    fun getOrCreateToken(context: Context): String {
        val preferences = context.getSharedPreferences(
            PREFS_NAME,
            Context.MODE_PRIVATE
        )

        val existing = preferences.getString(TOKEN_KEY, null)

        if (!existing.isNullOrBlank()) {
            return existing
        }

        val bytes = ByteArray(32)
        secureRandom.nextBytes(bytes)

        val token = Base64.encodeToString(
            bytes,
            Base64.NO_WRAP or Base64.NO_PADDING or Base64.URL_SAFE
        )

        val committed = preferences
            .edit()
            .putString(TOKEN_KEY, token)
            .commit()

        if (!committed) {
            throw IllegalStateException(
                "Unable to persist bridge authentication token"
            )
        }

        Log.i(TAG, "Bridge authentication token created")

        return token
    }

    fun newRequestId(): String {
        return UUID.randomUUID().toString()
    }

    fun success(
        requestId: String,
        data: JSONObject
    ): JSONObject {
        return JSONObject()
            .put("protocol", PROTOCOL)
            .put("version", VERSION)
            .put("ok", true)
            .put("request_id", requestId)
            .put("data", data)
    }

    fun error(
        requestId: String,
        code: String,
        message: String
    ): JSONObject {
        return JSONObject()
            .put("protocol", PROTOCOL)
            .put("version", VERSION)
            .put("ok", false)
            .put("request_id", requestId)
            .put(
                "error",
                JSONObject()
                    .put("code", code)
                    .put("message", message)
            )
    }

    fun parseRequest(body: String): BridgeRequest {
        if (body.isBlank()) {
            throw BridgeProtocolException(
                "EMPTY_BODY",
                "Request body is empty"
            )
        }

        val json = try {
            JSONObject(body)
        } catch (exception: Exception) {
            throw BridgeProtocolException(
                "INVALID_JSON",
                "Request body is not valid JSON"
            )
        }

        val protocol = json.optString("protocol", "")

        if (protocol != PROTOCOL) {
            throw BridgeProtocolException(
                "UNSUPPORTED_PROTOCOL",
                "Unsupported protocol"
            )
        }

        val version = json.optString("version", "")

        if (version != VERSION) {
            throw BridgeProtocolException(
                "UNSUPPORTED_VERSION",
                "Unsupported protocol version"
            )
        }

        val requestId = json.optString("request_id", "")

        if (requestId.isBlank() || requestId.length > 128) {
            throw BridgeProtocolException(
                "INVALID_REQUEST_ID",
                "request_id is required and must not exceed 128 characters"
            )
        }

        val command = json.optString("command", "")

        if (command.isBlank() || command.length > 64) {
            throw BridgeProtocolException(
                "INVALID_COMMAND",
                "command is required and must not exceed 64 characters"
            )
        }

        val args = json.optJSONObject("args") ?: JSONObject()

        return BridgeRequest(
            protocol = protocol,
            version = version,
            requestId = requestId,
            command = command,
            args = args
        )
    }

    fun uiNodeToJson(
        node: AgentAccessibilityService.UiNodeData
    ): JSONObject {
        val children = JSONArray()

        for (child in node.children) {
            children.put(uiNodeToJson(child))
        }

        return JSONObject()
            .put("index", node.index)
            .put("depth", node.depth)
            .putNullable("class_name", node.className)
            .putNullable("package_name", node.packageName)
            .putNullable("view_id_resource_name", node.viewIdResourceName)
            .putNullable("text", node.text)
            .putNullable("content_description", node.contentDescription)
            .put("clickable", node.clickable)
            .put("enabled", node.enabled)
            .put("focusable", node.focusable)
            .put("focused", node.focused)
            .put("scrollable", node.scrollable)
            .put("selected", node.selected)
            .put("checked", node.checked)
            .put("editable", node.editable)
            .put("visible_to_user", node.visibleToUser)
            .put(
                "bounds",
                JSONObject()
                    .put("left", node.boundsLeft)
                    .put("top", node.boundsTop)
                    .put("right", node.boundsRight)
                    .put("bottom", node.boundsBottom)
            )
            .put("children", children)
    }

    private fun JSONObject.putNullable(
        key: String,
        value: String?
    ): JSONObject {
        return if (value == null) {
            put(key, JSONObject.NULL)
        } else {
            put(key, value)
        }
    }

    data class BridgeRequest(
        val protocol: String,
        val version: String,
        val requestId: String,
        val command: String,
        val args: JSONObject
    )

    class BridgeProtocolException(
        val code: String,
        override val message: String
    ) : Exception(message)
}
