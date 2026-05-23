package com.fangxiaonan.cccompanion.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Code
import androidx.compose.material.icons.outlined.Chat
import androidx.compose.material.icons.outlined.Code
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.fangxiaonan.cccompanion.ui.chat.ChatScreen
import com.fangxiaonan.cccompanion.ui.settings.SettingsScreen
import com.fangxiaonan.cccompanion.ui.terminal.TerminalScreen

enum class Screen(val title: String, val filledIcon: ImageVector, val outlinedIcon: ImageVector) {
    Chat("Chat", Icons.Filled.Chat, Icons.Outlined.Chat),
    Terminal("Terminal", Icons.Filled.Code, Icons.Outlined.Code),
    Settings("Settings", Icons.Filled.Settings, Icons.Outlined.Settings)
}

@Composable
private fun CustomBottomBar(
    currentScreen: Screen,
    onSelect: (Screen) -> Unit
) {
    Surface(
        color = Color(0xFF1A1A2E),
        tonalElevation = 0.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .height(72.dp),
            horizontalArrangement = Arrangement.SpaceAround,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Screen.entries.forEach { screen ->
                key(screen) {
                    val selected = currentScreen == screen
                    val interactionSource = remember { MutableInteractionSource() }

                    Column(
                        modifier = Modifier
                            .weight(1f)
                            .fillMaxHeight()
                            .clickable(
                                interactionSource = interactionSource,
                                indication = null
                            ) {
                                onSelect(screen)
                            },
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.Center
                    ) {
                        Icon(
                            imageVector = if (selected) screen.filledIcon else screen.outlinedIcon,
                            contentDescription = screen.title,
                            tint = if (selected) Color.White else Color.White.copy(alpha = 0.35f),
                            modifier = Modifier.size(26.dp)
                        )

                        Spacer(modifier = Modifier.height(4.dp))

                        Text(
                            text = screen.title,
                            style = MaterialTheme.typography.labelSmall,
                            color = if (selected) Color(0xFF8B7AB8) else Color.White.copy(alpha = 0.35f)
                        )
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen() {
    var currentScreen by remember { mutableStateOf(Screen.Chat) }

    Scaffold(
        bottomBar = {
            CustomBottomBar(
                currentScreen = currentScreen,
                onSelect = { currentScreen = it }
            )
        }
    ) { innerPadding ->
        when (currentScreen) {
            Screen.Chat -> ChatScreen(modifier = Modifier.padding(innerPadding))
            Screen.Terminal -> TerminalScreen(modifier = Modifier.padding(innerPadding))
            Screen.Settings -> SettingsScreen(modifier = Modifier.padding(innerPadding))
        }
    }
}
