package com.fangxiaonan.cccompanion.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.painter.Painter
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.material3.LocalTextStyle
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.heightIn
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.viewmodel.compose.viewModel
import com.fangxiaonan.cccompanion.CcCompanionApp
import com.fangxiaonan.cccompanion.data.ChatMessage
import io.noties.markwon.Markwon
import io.noties.markwon.ext.strikethrough.StrikethroughPlugin
import io.noties.markwon.ext.tables.TablePlugin
import kotlinx.coroutines.launch
import org.json.JSONArray

fun parseHexColor(hex: String): Color {
    return try {
        Color(("FF" + hex.removePrefix("#").uppercase()).toLong(16))
    } catch (_: Exception) {
        Color(0xFF5B4A8A)
    }
}

@Composable
fun rememberPaperPlaneIcon(): Painter {
    val icon = remember {
        ImageVector.Builder(
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f
        ).addPath(
            pathData = PathParser().parsePathString(
                "M20.9 3.7C21.38 3.51 21.86 3.99 21.67 4.47L15.35 20.14C15.16 20.61 14.55 20.72 14.2 20.36L10.1 16.16C9.87 15.93 9.85 15.57 10.05 15.31L15.35 8.65L8.69 13.95C8.43 14.15 8.07 14.13 7.84 13.9L3.64 9.8C3.28 9.45 3.39 8.84 3.86 8.65L20.9 3.7Z"
            ).toNodes(),
            fill = androidx.compose.ui.graphics.SolidColor(Color.White)
        ).build()
    }
    return rememberVectorPainter(icon)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    modifier: Modifier = Modifier,
    viewModel: ChatViewModel = viewModel()
) {
    val messages by viewModel.messages.collectAsState()
    val isTyping by viewModel.isTyping.collectAsState()
    val isConnected by viewModel.isConnected.collectAsState()
    val error by viewModel.error.collectAsState()
    val isSending by viewModel.isSending.collectAsState()

    val context = LocalContext.current
    val prefs = (context.applicationContext as CcCompanionApp).preferences
    val bubbleColor = remember { mutableStateOf(parseHexColor(prefs.bubbleColor)) }
    val assistantBubbleColor = remember { mutableStateOf(parseHexColor(prefs.assistantBubbleColor)) }
    val bubbleFontSize = remember { mutableIntStateOf(prefs.bubbleFontSize) }
    val userTextColor = remember { mutableStateOf(parseHexColor(prefs.userTextColor)) }
    val assistantTextColor = remember { mutableStateOf(parseHexColor(prefs.assistantTextColor)) }

    LaunchedEffect(messages.size) {
        bubbleColor.value = parseHexColor(prefs.bubbleColor)
        assistantBubbleColor.value = parseHexColor(prefs.assistantBubbleColor)
        bubbleFontSize.intValue = prefs.bubbleFontSize
        userTextColor.value = parseHexColor(prefs.userTextColor)
        assistantTextColor.value = parseHexColor(prefs.assistantTextColor)
    }

    var inputText by remember { mutableStateOf("") }
    val listState = rememberLazyListState()
    val coroutineScope = rememberCoroutineScope()
    val focusManager = LocalFocusManager.current

    // Start/stop polling with lifecycle
    DisposableEffect(Unit) {
        viewModel.startPolling()
        onDispose {
            viewModel.stopPolling()
        }
    }

    // Auto-scroll to bottom when new messages arrive
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) {
            try {
                listState.scrollToItem(messages.size - 1)
            } catch (_: Exception) {}
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
    ) {
        // Status bar - subtle, blends with theme
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(MaterialTheme.colorScheme.background)
                .padding(horizontal = 16.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(6.dp)
                        .clip(CircleShape)
                        .background(
                            if (isConnected) Color(0xFF39D353) else Color(0xFFEF4444)
                        )
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = if (isConnected) "Connected" else "Disconnected",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f)
                )
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                AnimatedVisibility(visible = isTyping) {
                    Text(
                        text = "Claude is typing...",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary.copy(alpha = 0.7f)
                    )
                }
                Spacer(modifier = Modifier.width(4.dp))
                IconButton(
                    onClick = { viewModel.refresh() },
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(
                        Icons.Default.Refresh,
                        contentDescription = "Refresh",
                        modifier = Modifier.size(16.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                    )
                }
            }
        }

        // Error banner
        AnimatedVisibility(visible = error != null) {
            Surface(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 4.dp),
                shape = RoundedCornerShape(12.dp),
                color = MaterialTheme.colorScheme.errorContainer
            ) {
                Text(
                    text = error ?: "",
                    modifier = Modifier.padding(12.dp),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }

        // Messages list - more breathing room
        LazyColumn(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 12.dp),
            state = listState,
            verticalArrangement = Arrangement.spacedBy(10.dp),
            contentPadding = PaddingValues(vertical = 12.dp)
        ) {
            items(messages, key = { it.id }) { message ->
                MessageBubble(
                    message = message,
                    userBubbleColor = bubbleColor.value,
                    assistantBubbleColor = assistantBubbleColor.value,
                    bodyFontSize = bubbleFontSize.intValue.sp,
                    thinkingFontSize = (bubbleFontSize.intValue - 0.5f).sp,
                    userTextColor = userTextColor.value,
                    assistantTextColor = assistantTextColor.value
                )
            }
        }

        // Input area - dark, rounded, themed
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .background(MaterialTheme.colorScheme.background)
                .padding(horizontal = 12.dp, vertical = 10.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                TextField(
                    value = inputText,
                    onValueChange = { inputText = it },
                    modifier = Modifier.weight(1f).heightIn(min = 36.dp),
                    placeholder = {
                        Text(
                            "Message or /command...",
                            color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.4f)
                        )
                    },
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(
                        onSend = {
                            if (inputText.isNotBlank() && !isSending) {
                                val msg = inputText
                                inputText = ""
                                viewModel.sendMessage(msg)
                            }
                        }
                    ),
                    maxLines = 5,
                    singleLine = false,
                    shape = RoundedCornerShape(20.dp),
                    textStyle = LocalTextStyle.current.copy(fontSize = bubbleFontSize.intValue.sp, lineHeight = (bubbleFontSize.intValue + 4).sp),
                    colors = TextFieldDefaults.colors(
                        focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.7f),
                        unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
                        focusedIndicatorColor = Color.Transparent,
                        unfocusedIndicatorColor = Color.Transparent,
                        disabledIndicatorColor = Color.Transparent,
                        focusedTextColor = MaterialTheme.colorScheme.onSurface,
                        unfocusedTextColor = MaterialTheme.colorScheme.onSurface,
                        cursorColor = MaterialTheme.colorScheme.primary,
                    )
                )
                Spacer(modifier = Modifier.width(10.dp))
                FilledIconButton(
                    onClick = {
                        if (inputText.isNotBlank() && !isSending) {
                            val msg = inputText
                            inputText = ""
                            viewModel.sendMessage(msg)
                        }
                    },
                    enabled = inputText.isNotBlank() && !isSending,
                    modifier = Modifier.size(36.dp),
                    shape = CircleShape,
                    colors = IconButtonDefaults.filledIconButtonColors(
                        containerColor = bubbleColor.value,
                        contentColor = Color.White,
                        disabledContainerColor = Color(0xFF3A3A4A),
                        disabledContentColor = Color(0xFF6A6A7A)
                    )
                ) {
                    if (isSending) {
                        Text("···", fontSize = 16.sp, color = Color.White.copy(alpha = 0.7f))
                    } else {
                        Icon(
                            painter = rememberPaperPlaneIcon(),
                            contentDescription = "Send",
                            modifier = Modifier
                                .size(28.dp)
                                .offset(x = (-2).dp, y = 1.dp),
                            tint = Color.White
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun MessageBubble(
    message: ChatMessage,
    userBubbleColor: Color = Color(0xFF5B4A8A),
    assistantBubbleColor: Color = Color(0xFF262638),
    bodyFontSize: TextUnit = 14.sp,
    thinkingFontSize: TextUnit = 12.sp,
    userTextColor: Color = Color(0xFFF0E8FF),
    assistantTextColor: Color = Color(0xFFD8D8E8)
) {
    val isUser = message.role == "user"
    val isSystem = message.role == "system"

    val context = LocalContext.current

    val alignment = when {
        isUser -> Alignment.End
        else -> Alignment.Start
    }

    // Bubble shapes with large rounded corners (20dp), smaller on the tail side
    val shape = when {
        isUser -> RoundedCornerShape(20.dp, 20.dp, 6.dp, 20.dp)
        else -> RoundedCornerShape(20.dp, 20.dp, 20.dp, 6.dp)
    }

    // Colors
    val bubbleColor = when {
        isSystem -> MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f)
        isUser -> userBubbleColor
        else -> assistantBubbleColor
    }

    val textColor = when {
        isSystem -> MaterialTheme.colorScheme.onSurfaceVariant
        isUser -> userTextColor
        else -> assistantTextColor
    }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = alignment
    ) {
        if (isUser) {
            Box(
                modifier = Modifier
                    .widthIn(max = 320.dp)
                    .clip(shape)
                    .background(userBubbleColor)
                    .padding(horizontal = 14.dp, vertical = 10.dp)
            ) {
                SelectionContainer {
                    Text(
                        text = message.text,
                        style = MaterialTheme.typography.bodyMedium.copy(
                            fontFamily = if (message.text.contains("```")) FontFamily.Monospace else FontFamily.Default
                        ),
                        color = textColor,
                        fontSize = bodyFontSize,
                        lineHeight = (bodyFontSize.value + 6).sp
                    )
                }
            }
        } else {
            Surface(
                modifier = Modifier.widthIn(max = 320.dp),
                shape = shape,
                color = bubbleColor
            ) {
                Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                    // Collapsible thinking chain
                    if (message.thinking.isNotBlank()) {
                        var thinkingExpanded by remember { mutableStateOf(false) }

                        Text(
                            text = if (thinkingExpanded) "🧠 思考过程 ▲" else "🧠 思考过程 ▼",
                            modifier = Modifier
                                .clickable { thinkingExpanded = !thinkingExpanded }
                                .padding(bottom = 6.dp),
                            color = Color(0xFF9898B0),
                            fontSize = thinkingFontSize,
                            lineHeight = (thinkingFontSize.value + 4).sp
                        )

                        AnimatedVisibility(
                            visible = thinkingExpanded,
                            enter = expandVertically(),
                            exit = shrinkVertically()
                        ) {
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(bottom = 8.dp)
                                    .background(
                                        Color(0xFF1E1E2E),
                                        shape = RoundedCornerShape(8.dp)
                                    )
                                    .padding(10.dp)
                            ) {
                                SelectionContainer {
                                    Text(
                                        text = message.thinking,
                                        color = Color(0xFF9898B0),
                                        fontSize = thinkingFontSize,
                                        lineHeight = (thinkingFontSize.value + 4).sp,
                                        fontFamily = FontFamily.Monospace
                                    )
                                }
                            }
                        }
                    }

                    // Collapsible tool calls
                    if (message.tools.isNotBlank()) {
                        var toolsExpanded by remember { mutableStateOf(false) }

                        Text(
                            text = if (toolsExpanded) "🔧 工具调用 ▲" else "🔧 工具调用 ▼",
                            modifier = Modifier
                                .clickable { toolsExpanded = !toolsExpanded }
                                .padding(bottom = 6.dp),
                            color = Color(0xFFB0B098),
                            fontSize = thinkingFontSize,
                            lineHeight = (thinkingFontSize.value + 4).sp
                        )

                        AnimatedVisibility(
                            visible = toolsExpanded,
                            enter = expandVertically(),
                            exit = shrinkVertically()
                        ) {
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(bottom = 8.dp)
                                    .background(
                                        Color(0xFF2E2E1E),
                                        shape = RoundedCornerShape(8.dp)
                                    )
                                    .padding(10.dp)
                            ) {
                                SelectionContainer {
                                    Column {
                                        val toolsList = parseToolCalls(message.tools)
                                        toolsList.forEach { tool ->
                                            Text(
                                                text = "📎 ${tool.first}: ${tool.second}",
                                                color = Color(0xFFB0B098),
                                                fontSize = thinkingFontSize,
                                                lineHeight = (thinkingFontSize.value + 4).sp,
                                                fontFamily = FontFamily.Monospace,
                                                maxLines = 2,
                                                overflow = TextOverflow.Ellipsis,
                                                modifier = Modifier.padding(bottom = 4.dp)
                                            )
                                        }
                                        if (toolsList.isEmpty()) {
                                            Text(
                                                text = message.tools,
                                                color = Color(0xFFB0B098),
                                                fontSize = thinkingFontSize,
                                                lineHeight = (thinkingFontSize.value + 4).sp,
                                                fontFamily = FontFamily.Monospace
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Markdown rendering for assistant messages
                    val markwon = remember {
                        Markwon.builder(context)
                            .usePlugin(StrikethroughPlugin.create())
                            .usePlugin(TablePlugin.create(context))
                            .build()
                    }

                    val textColorInt = remember(textColor) {
                        android.graphics.Color.rgb(
                            (textColor.red * 255).toInt(),
                            (textColor.green * 255).toInt(),
                            (textColor.blue * 255).toInt()
                        )
                    }

                    val bodyFontSizePx = bodyFontSize.value

                    AndroidView(
                        factory = { ctx ->
                            android.widget.TextView(ctx).apply {
                                setTextColor(textColorInt)
                                textSize = bodyFontSizePx
                                setLineSpacing(0f, 1.3f)
                                isClickable = false
                                movementMethod = null
                            }
                        },
                        update = { tv ->
                            tv.setTextColor(textColorInt)
                            tv.textSize = bodyFontSizePx
                            markwon.setMarkdown(tv, message.text)
                        },
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            }
        }
    }
}

/**
 * Parse tool calls JSON array string into list of (name, input_preview) pairs.
 */
fun parseToolCalls(toolsJson: String): List<Pair<String, String>> {
    return try {
        val arr = JSONArray(toolsJson)
        val result = mutableListOf<Pair<String, String>>()
        for (i in 0 until arr.length()) {
            val obj = arr.getJSONObject(i)
            val name = obj.optString("name", "unknown")
            val preview = obj.optString("input_preview", "")
            result.add(Pair(name, preview))
        }
        result
    } catch (e: Exception) {
        emptyList()
    }
}
