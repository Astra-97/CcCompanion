package com.fangxiaonan.cccompanion.ui.settings

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.fangxiaonan.cccompanion.CcCompanionApp
import com.fangxiaonan.cccompanion.data.ApiClient
import com.fangxiaonan.cccompanion.ui.chat.parseHexColor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlin.math.roundToInt

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val prefs = (context.applicationContext as CcCompanionApp).preferences
    val coroutineScope = rememberCoroutineScope()

    var serverUrl by remember { mutableStateOf(prefs.serverUrl) }
    var authToken by remember { mutableStateOf(prefs.authToken) }
    var showToken by remember { mutableStateOf(false) }
    var saved by remember { mutableStateOf(false) }
    var healthStatus by remember { mutableStateOf<String?>(null) }
    var isChecking by remember { mutableStateOf(false) }

    val textFieldColors = TextFieldDefaults.colors(
        focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
        unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.3f),
        focusedIndicatorColor = Color.Transparent,
        unfocusedIndicatorColor = Color.Transparent,
        disabledIndicatorColor = Color.Transparent,
        focusedTextColor = MaterialTheme.colorScheme.onSurface,
        unfocusedTextColor = MaterialTheme.colorScheme.onSurface,
        cursorColor = MaterialTheme.colorScheme.primary,
        focusedLabelColor = MaterialTheme.colorScheme.primary,
        unfocusedLabelColor = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f),
    )

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        Text(
            text = "Settings",
            style = MaterialTheme.typography.headlineMedium,
            color = MaterialTheme.colorScheme.onBackground,
            modifier = Modifier.padding(bottom = 8.dp)
        )

        // Server URL
        TextField(
            value = serverUrl,
            onValueChange = { serverUrl = it; saved = false },
            label = { Text("Server URL") },
            placeholder = { Text("http://100.x.x.x:8795") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(14.dp),
            colors = textFieldColors,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri)
        )

        // Auth Token
        TextField(
            value = authToken,
            onValueChange = { authToken = it; saved = false },
            label = { Text("Auth Token") },
            placeholder = { Text("shared_secret") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(14.dp),
            colors = textFieldColors,
            visualTransformation = if (showToken) VisualTransformation.None else PasswordVisualTransformation(),
            trailingIcon = {
                IconButton(onClick = { showToken = !showToken }) {
                    Icon(
                        if (showToken) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                        contentDescription = "Toggle visibility",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                    )
                }
            },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password)
        )

        Button(
            onClick = {
                prefs.serverUrl = serverUrl.trimEnd('/')
                prefs.authToken = authToken.trim()
                saved = true
            },
            modifier = Modifier.fillMaxWidth().height(48.dp),
            shape = RoundedCornerShape(14.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = parseHexColor(prefs.bubbleColor),
                contentColor = Color.White
            )
        ) {
            Icon(Icons.Default.Check, contentDescription = null, modifier = Modifier.size(18.dp))
            Spacer(modifier = Modifier.width(8.dp))
            Text("Save Settings")
        }

        if (saved) {
            Text("Settings saved", color = Color(0xFF39D353), style = MaterialTheme.typography.bodySmall)
        }

        Divider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f), modifier = Modifier.padding(vertical = 4.dp))

        // ── Bubble Settings ──
        Text(
            text = "Bubble Settings",
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onBackground
        )

        // Font size slider
        var fontSlider by remember { mutableFloatStateOf(prefs.bubbleFontSize.toFloat()) }

        Text(
            text = "Font Size: ${fontSlider.roundToInt()}sp",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Slider(
            value = fontSlider,
            onValueChange = { fontSlider = it },
            onValueChangeFinished = { prefs.bubbleFontSize = fontSlider.roundToInt() },
            valueRange = 10f..22f,
            steps = 11,
            modifier = Modifier.fillMaxWidth(),
            colors = SliderDefaults.colors(
                thumbColor = parseHexColor(prefs.bubbleColor),
                activeTrackColor = parseHexColor(prefs.bubbleColor)
            )
        )

        // Preview text
        Surface(
            shape = RoundedCornerShape(20.dp),
            color = Color(0xFF262638)
        ) {
            Text(
                text = "Preview: The quick brown fox",
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                color = Color(0xFFD8D8E8),
                fontSize = fontSlider.roundToInt().sp
            )
        }

        Spacer(modifier = Modifier.height(8.dp))

        // Bubble color hex input
        var hexInput by remember { mutableStateOf(prefs.bubbleColor) }
        var colorPreview by remember { mutableStateOf(parseHexColor(prefs.bubbleColor)) }

        Text(
            text = "Bubble Color",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(colorPreview)
            )

            TextField(
                value = hexInput,
                onValueChange = { input ->
                    val cleaned = input.removePrefix("#").filter { it in "0123456789ABCDEFabcdef" }.take(6)
                    hexInput = cleaned
                    if (cleaned.length == 6) {
                        colorPreview = parseHexColor(cleaned)
                        prefs.bubbleColor = cleaned
                    }
                },
                label = { Text("#HEX") },
                placeholder = { Text("5B4A8A") },
                modifier = Modifier.weight(1f),
                singleLine = true,
                shape = RoundedCornerShape(14.dp),
                colors = textFieldColors,
                prefix = { Text("#", color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)) }
            )
        }

        // User bubble preview
        Surface(
            shape = RoundedCornerShape(20.dp, 20.dp, 6.dp, 20.dp),
            color = colorPreview
        ) {
            Text(
                text = "User bubble preview",
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                color = Color(0xFFF0E8FF),
                fontSize = fontSlider.roundToInt().sp
            )
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Assistant bubble color
        var assistantHex by remember { mutableStateOf(prefs.assistantBubbleColor) }
        var assistantPreview by remember { mutableStateOf(parseHexColor(prefs.assistantBubbleColor)) }

        Text(
            text = "Assistant Bubble Color",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(assistantPreview)
            )

            TextField(
                value = assistantHex,
                onValueChange = { input ->
                    val cleaned = input.removePrefix("#").filter { it in "0123456789ABCDEFabcdef" }.take(6)
                    assistantHex = cleaned
                    if (cleaned.length == 6) {
                        assistantPreview = parseHexColor(cleaned)
                        prefs.assistantBubbleColor = cleaned
                    }
                },
                label = { Text("#HEX") },
                placeholder = { Text("262638") },
                modifier = Modifier.weight(1f),
                singleLine = true,
                shape = RoundedCornerShape(14.dp),
                colors = textFieldColors,
                prefix = { Text("#", color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)) }
            )
        }

        // Assistant bubble preview
        Surface(
            shape = RoundedCornerShape(20.dp, 20.dp, 20.dp, 6.dp),
            color = assistantPreview
        ) {
            Text(
                text = "Assistant bubble preview",
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                color = Color(0xFFD8D8E8),
                fontSize = fontSlider.roundToInt().sp
            )
        }

        Spacer(modifier = Modifier.height(12.dp))

        // User Text Color
        var userTextHex by remember { mutableStateOf(prefs.userTextColor) }
        var userTextPreview by remember { mutableStateOf(parseHexColor(prefs.userTextColor)) }

        Text(
            text = "User Text Color",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(userTextPreview)
            )

            TextField(
                value = userTextHex,
                onValueChange = { input ->
                    val cleaned = input.removePrefix("#").filter { it in "0123456789ABCDEFabcdef" }.take(6)
                    userTextHex = cleaned
                    if (cleaned.length == 6) {
                        userTextPreview = parseHexColor(cleaned)
                        prefs.userTextColor = cleaned
                    }
                },
                label = { Text("#HEX") },
                placeholder = { Text("F0E8FF") },
                modifier = Modifier.weight(1f),
                singleLine = true,
                shape = RoundedCornerShape(14.dp),
                colors = textFieldColors,
                prefix = { Text("#", color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)) }
            )
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Assistant Text Color
        var assistantTextHex by remember { mutableStateOf(prefs.assistantTextColor) }
        var assistantTextPreview by remember { mutableStateOf(parseHexColor(prefs.assistantTextColor)) }

        Text(
            text = "Assistant Text Color",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(assistantTextPreview)
            )

            TextField(
                value = assistantTextHex,
                onValueChange = { input ->
                    val cleaned = input.removePrefix("#").filter { it in "0123456789ABCDEFabcdef" }.take(6)
                    assistantTextHex = cleaned
                    if (cleaned.length == 6) {
                        assistantTextPreview = parseHexColor(cleaned)
                        prefs.assistantTextColor = cleaned
                    }
                },
                label = { Text("#HEX") },
                placeholder = { Text("D8D8E8") },
                modifier = Modifier.weight(1f),
                singleLine = true,
                shape = RoundedCornerShape(14.dp),
                colors = textFieldColors,
                prefix = { Text("#", color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)) }
            )
        }

        Divider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f), modifier = Modifier.padding(vertical = 4.dp))

        // Connection Test
        Text(
            text = "Connection Test",
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onBackground
        )

        OutlinedButton(
            onClick = {
                isChecking = true
                healthStatus = null
                coroutineScope.launch(Dispatchers.IO) {
                    val client = ApiClient(baseUrl = serverUrl.trimEnd('/'), authToken = authToken.trim())
                    val result = client.healthCheck()
                    result.onSuccess { healthy ->
                        healthStatus = if (healthy) "Connected successfully!" else "Server returned unhealthy status"
                    }.onFailure {
                        healthStatus = "Connection failed: ${it.message}"
                    }
                    isChecking = false
                }
            },
            modifier = Modifier.fillMaxWidth().height(48.dp),
            enabled = serverUrl.isNotBlank() && !isChecking,
            shape = RoundedCornerShape(14.dp),
            colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.primary),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.5f))
        ) {
            if (isChecking) { Text("···", color = MaterialTheme.colorScheme.primary); Spacer(modifier = Modifier.width(8.dp)) }
            Text("Test Connection")
        }

        healthStatus?.let { status ->
            Text(
                text = status,
                color = if (status.startsWith("Connected")) Color(0xFF39D353) else MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall
            )
        }

        Divider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f), modifier = Modifier.padding(vertical = 4.dp))

        Text(
            text = "About",
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onBackground
        )

        Surface(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(14.dp),
            color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.3f)
        ) {
            Text(
                text = "CC Companion v0.3.0\n\n" +
                        "Mobile client for CcCompanion server.\n" +
                        "Connect via Tailscale for secure access.\n\n" +
                        "Slash commands in chat:\n" +
                        "  /new - New session\n" +
                        "  /stop - Abort current task\n" +
                        "  /restart - Restart Claude\n" +
                        "  /compact - Compact conversation",
                modifier = Modifier.padding(16.dp),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f),
                lineHeight = 18.sp
            )
        }

        Spacer(modifier = Modifier.height(16.dp))
    }
}
