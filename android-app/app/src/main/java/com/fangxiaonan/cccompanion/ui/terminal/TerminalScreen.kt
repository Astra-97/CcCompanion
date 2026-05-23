package com.fangxiaonan.cccompanion.ui.terminal

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TerminalScreen(
    modifier: Modifier = Modifier,
    viewModel: TerminalViewModel = viewModel()
) {
    val terminalOutput by viewModel.terminalOutput.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()

    DisposableEffect(Unit) {
        viewModel.startPolling()
        onDispose {
            viewModel.stopPolling()
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(Color(0xFF0D1117))
    ) {
        // Top bar - minimal, blends with terminal
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Color(0xFF0D1117))
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(
                text = "Terminal",
                style = MaterialTheme.typography.titleMedium,
                color = Color(0xFF39D353).copy(alpha = 0.8f),
                fontFamily = FontFamily.Monospace
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                if (isLoading) {
                    Text("···", color = Color(0xFF39D353).copy(alpha = 0.5f), fontSize = 14.sp)
                    Spacer(modifier = Modifier.width(12.dp))
                }
                IconButton(
                    onClick = { viewModel.manualRefresh() },
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(
                        Icons.Default.Refresh,
                        contentDescription = "Refresh",
                        modifier = Modifier.size(18.dp),
                        tint = Color(0xFF39D353).copy(alpha = 0.5f)
                    )
                }
            }
        }

        // Error
        if (error != null) {
            Surface(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 4.dp),
                shape = RoundedCornerShape(8.dp),
                color = Color(0xFF3D1A1A)
            ) {
                Text(
                    text = error ?: "",
                    modifier = Modifier.padding(10.dp),
                    style = MaterialTheme.typography.bodySmall,
                    color = Color(0xFFFFB4AB),
                    fontFamily = FontFamily.Monospace
                )
            }
        }

        // Terminal content
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 8.dp, vertical = 4.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(Color(0xFF0D1117))
                .padding(10.dp)
        ) {
            val verticalScrollState = rememberScrollState()
            val horizontalScrollState = rememberScrollState()

            // Auto-scroll to bottom
            LaunchedEffect(terminalOutput) {
                verticalScrollState.animateScrollTo(verticalScrollState.maxValue)
            }

            Text(
                text = terminalOutput.ifEmpty { "$ waiting for output..." },
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(verticalScrollState)
                    .horizontalScroll(horizontalScrollState),
                fontFamily = FontFamily.Monospace,
                fontSize = 12.sp,
                color = Color(0xFF39D353),
                lineHeight = 16.sp,
                letterSpacing = 0.3.sp
            )
        }
    }
}
