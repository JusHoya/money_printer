import ctypes
import os

# Windows API Constants
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def prevent_sleep():
    """
    Prevents the Windows OS from going to sleep or turning off the display.
    Effective only while the process is running.
    """
    if os.name != "nt":
        print("[System] Sleep prevention only supported on Windows.")
        return

    print("[System] Insomniac Protocol engaged. System will not sleep.")
    try:
        # Prevent Sleep & Display Turn Off
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception as e:
        print(f"[System] Failed to set execution state: {e}")


def allow_sleep():
    """
    Resets the execution state to allow the system to sleep normally.
    """
    if os.name != "nt":
        return

    print("[System] Insomniac Protocol disengaged. Sleep allowed.")
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as e:
        print(f"[System] Failed to reset execution state: {e}")
