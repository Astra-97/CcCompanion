package com.fangxiaonan.cccompanion.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// Deep navy dark theme palette — matched to approved HTML preview
private val DarkColorScheme = darkColorScheme(
    primary = Color(0xFF8B7AB8),          // Muted purple (nav active color)
    secondary = Color(0xFF5B4A8A),         // Muted purple for buttons/accents
    tertiary = Color(0xFF4E3D7A),          // Deeper muted purple
    background = Color(0xFF1A1A2E),        // Deep navy
    surface = Color(0xFF1A1A2E),           // Same as background for cohesion
    surfaceVariant = Color(0xFF262638),    // Input field / assistant bubble bg
    onPrimary = Color.White,
    onSecondary = Color.White,
    onBackground = Color(0xFFE2E2F0),      // Slightly cool white
    onSurface = Color(0xFFE2E2F0),
    onSurfaceVariant = Color(0xFFA0A0B8),  // Muted cool gray
    primaryContainer = Color(0xFF5B4A8A),  // User bubble muted purple
    onPrimaryContainer = Color(0xFFE8E0F4),// User bubble text
    secondaryContainer = Color(0xFF262638),// Assistant bubble dark blue-gray
    onSecondaryContainer = Color(0xFFC8C8D8),// Assistant bubble text
    errorContainer = Color(0xFF3D1A1A),
    onErrorContainer = Color(0xFFFFB4AB),
    outline = Color(0xFF3A3A52),           // Subtle borders
    outlineVariant = Color(0xFF2E2E45),
    surfaceTint = Color(0xFF8B7AB8),
)

private val LightColorScheme = lightColorScheme(
    primary = Color(0xFF5B4A8A),
    secondary = Color(0xFF6D28D9),
    tertiary = Color(0xFF5B21B6),
    background = Color(0xFFFAF9FC),
    surface = Color(0xFFFAF9FC),
    onPrimary = Color.White,
    onSecondary = Color.White,
    onBackground = Color(0xFF1C1B1F),
    onSurface = Color(0xFF1C1B1F),
)

@Composable
fun CcCompanionTheme(
    darkTheme: Boolean = true, // Default to dark theme
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            window.statusBarColor = colorScheme.background.toArgb()
            window.navigationBarColor = colorScheme.background.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !darkTheme
            WindowCompat.getInsetsController(window, view).isAppearanceLightNavigationBars = !darkTheme
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        content = content
    )
}
