package com.ai.agentpro

import android.graphics.Bitmap

/**
 * dHash (difference hash) of a frame.
 *
 * The frame is reduced to a [size] x [size] greyscale grid, and each cell records
 * whether its right-hand neighbour is brighter. Comparing two hashes by their
 * Hamming distance then answers "did anything move on screen" without shipping
 * the pixels anywhere.
 *
 * Why not compare raw pixels: JPEG re-encoding alone made an idle device screen
 * differ by 0.65 in a naive per-pixel metric, so every such comparison reported
 * motion that never happened. A gradient hash is invariant to that, and it is
 * computed on device so the Python side needs no image library at all.
 */
object VisualHash {

    fun compute(bitmap: Bitmap, size: Int = 16): String {
        require(size in 2..64) { "size must be within 2..64, was $size" }

        val width = size + 1
        val height = size

        val pixels = IntArray(width * height)

        val scaled = Bitmap.createScaledBitmap(bitmap, width, height, true)

        try {
            scaled.getPixels(
                pixels,
                0,
                width,
                0,
                0,
                width,
                height
            )
        } finally {
            if (scaled !== bitmap) {
                scaled.recycle()
            }
        }

        val builder = StringBuilder(size * size * 2 / 8 + 2)

        for (row in 0 until height) {
            val base = row * width
            var nibble = 0
            var nibbleIndex = 0

            for (column in 0 until size) {
                if (luminance(pixels[base + column]) > luminance(pixels[base + column + 1])) {
                    nibble = nibble or (1 shl nibbleIndex)
                }

                nibbleIndex++

                if (nibbleIndex == 4) {
                    builder.append(HEX[nibble and 0x0F])
                    nibble = 0
                    nibbleIndex = 0
                }
            }

            if (nibbleIndex > 0) {
                builder.append(HEX[nibble and 0x0F])
            }
        }

        return builder.toString()
    }

    private fun luminance(color: Int): Int {
        val red = (color shr 16) and 0xFF
        val green = (color shr 8) and 0xFF
        val blue = color and 0xFF

        return (red * 77 + green * 150 + blue * 29) shr 8
    }

    private val HEX = "0123456789abcdef".toCharArray()
}

/** Outcome of a visual-hash request, kept apart from [ScreenshotEngine.CaptureResult]. */
data class VisualHashResult(
    val operationId: Long,
    val success: Boolean,
    val hash: String?,
    val code: String?,
    val message: String?
) {
    companion object {

        fun success(operationId: Long, hash: String): VisualHashResult {
            return VisualHashResult(
                operationId,
                true,
                hash,
                null,
                null
            )
        }

        fun failure(
            operationId: Long,
            code: String,
            message: String
        ): VisualHashResult {
            return VisualHashResult(
                operationId,
                false,
                null,
                code,
                message
            )
        }
    }
}
