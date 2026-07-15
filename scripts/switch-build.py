#!/usr/bin/env python3
"""
switch-build.py  —  one-shot Taisei (latest, v1.4.5) -> Switch .nro
                           built on devkitPro's SDL3 Switch backend.

This script builds directly against devkitPro's own SDL3 Switch fork, rather
than taking plain taisei-project/SDL and bolting a third-party Switch backend
(such as neomody77/sdl3-switch) onto it and hand-patching that backend:

    https://github.com/devkitPro/SDL/tree/switch-sdl-3.4   (SDL 3.4.0)

That branch ships a complete, maintained libnx backend (video/audio/joystick/
keyboard/mouse/touch/swkb/filesystem/power/time/timer), so almost none of the
SDL-side patching that a bolted-on third-party backend would need applies
here. Specifically, the following are already handled natively by devkitPro,
so no patch is applied for them:

  * GLES3 context     -- devkitPro's video driver uses SDL's core EGL path
                         (SDL_EGL_CreateContext_impl / SDL_egl.c), which already
                         honors the app's SDL_GL_SetAttribute() request, so
                         Taisei's ES 3.0 ask is granted with no backend patch.
  * clean exit        -- audren WaitDevice() already returns immediately and the
                         EGL teardown goes through SDL's standard destroy path;
                         no finite-wait / neutralized-teardown patch needed.
  * Nintendo A/B      -- devkitPro's joystick backend already maps face buttons
                         the swapped Nintendo way (a=1,b=0), matching the old
                         SDL2 port and Taisei's __SWITCH__ expectations.
  * EGL link + export -- devkitPro's CMakeLists already does
                         `sdl_link_dependency(opengl LIBS EGL stdc++ glapi
                         drm_nouveau)` on the Switch branch, which both links
                         SDL and writes those libs into sdl3.pc's Libs.private,
                         so Taisei resolves eglGetDisplay/etc automatically.
Two SDL-side patches ARE still needed and are applied to the SDL tree before it
is built:
  * SDL_egl.h    -- add a __SWITCH__ branch (see below).
  * audio driver -- devkitPro drives audio through audren (a software mixer).
                    Its audio-thread work (per-callback update + IPC + render-
                    frame waits) contends with Taisei's render thread on the
                    Switch's shared cores and causes frame dips, most visible
                    in handheld mode where the CPU/GPU budget is tightest. It
                    is replaced with the libnx AUDOUT backend (see
                    use_audout_audio_backend) -- PCM straight to the hardware
                    sink, with a 4-buffer cushion + shutdown-aware wait so
                    there is no BGM glitch.

On SDL_egl.h specifically: devkitPro's copy has
no __SWITCH__ branch in its EGL native-type block, so when Taisei's gles.c includes
<SDL3/SDL_egl.h> it falls through to `#error "Platform not recognized"`. A
Switch branch is added (see patch_sdl_egl_switch) before building SDL.

Everything on the *Taisei* side (wallpaper feature + fixes, VFS strfmt/mmap
bitrot, clean version label, NACP version) is game-side and independent of
which SDL backend is used, so it is applied verbatim regardless of source.

Run this in an empty folder on a Mac/Linux box that already has devkitPro at
/opt/devkitpro with the Switch portlibs installed (mesa, freetype, libpng, webp,
zstd, opus/ogg/opusfile, cglm, libzip, zlib) plus meson + ninja + cmake.

It will:
  1. clone devkitPro/SDL @ switch-sdl-3.4 (SDL 3.4.0, full libnx backend)
  2. cmake-build that SDL3 static and install it into your Switch portlibs
     (its sdl3.pc exports the mesa EGL deps for downstream linking)
  3. clone taisei @ v1.4.5 (recursive); it picks up the installed SDL 3.4.0 via
     pkg-config (satisfies its `sdl3 >= 3.2.0` requirement, no fallback wrap)
  4. apply the Taisei-side Switch fixes (wallpaper, VFS bitrot, version labels)
  5. meson/ninja-build the .nro
  6. drop the finished taisei.nro (+ SD-card layout) in ./dist/

Usage:
    python3 switch-build.py [--workdir DIR] [--jobs N] [--clean]
    DEVKITPRO=/opt/devkitpro is assumed; override with --devkitpro or $DEVKITPRO.

Copyright (C) 2026 ppkantorski <https://github.com/ppkantorski>
"""

import argparse, os, shutil, subprocess, sys, textwrap
from pathlib import Path

TAISEI_TAG  = "v1.4.5"
SDL_REPO    = "https://github.com/devkitPro/SDL"
SDL_BRANCH  = "switch-sdl-3.4"
TAISEI_REPO = "https://github.com/taisei-project/taisei"

def c(s, code):  # tiny color helper
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
def stage(msg): print("\n" + c(">> " + msg, "1;36"), flush=True)
def info(msg):  print(c("   " + msg, "0;37"), flush=True)
def die(msg):   print(c("!! " + msg, "1;31"), file=sys.stderr); sys.exit(1)

def run(cmd):
    info("$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    subprocess.run(cmd, shell=isinstance(cmd, str), check=True)

def bash(script, devkitpro=None):
    """Run a bash snippet with devkitPro's Switch environment sourced."""
    env = dict(os.environ)
    if devkitpro:
        env["DEVKITPRO"] = devkitpro
    full = 'set -euo pipefail\nsource "$DEVKITPRO/switchvars.sh"\n' + script
    info("$ (switchvars) " + script.strip().splitlines()[0][:100])
    subprocess.run(["/bin/bash", "-c", full], env=env, check=True)

def bash_capture(script, devkitpro):
    env = dict(os.environ); env["DEVKITPRO"] = devkitpro
    full = 'source "$DEVKITPRO/switchvars.sh"\n' + script
    return subprocess.run(["/bin/bash", "-c", full], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()

# ---------------------------------------------------------------------------
# The SDL-side patches devkitPro's tree still needs (headers + audio cushion).
# ---------------------------------------------------------------------------
def patch_sdl_egl_switch(sdl_root):
    """devkitPro's bundled SDL_egl.h has no branch for the Switch: its native-type
    typedef chain falls through to `#else / #error "Platform not recognized"`.
    SDL itself dodges this (its switch backend pulls the native types in another
    way), but when Taisei's src/renderer/glescommon/gles.c includes <SDL3/SDL_egl.h>
    it takes the builtin-definitions path and hits the #error, then errors again on
    every EGLNativeDisplayType/PixmapType/WindowType use. Add a __SWITCH__ branch
    (same shape as the __unix__/__Fuchsia__ branches) so the native types resolve.
    Applied to the SDL source tree *before* build+install, so the installed
    portlibs header carries the fix."""
    egl = sdl_root / "include/SDL3/SDL_egl.h"
    if not egl.exists():
        info("SDL_egl.h not found; skipping switch EGL fix"); return
    src = egl.read_text()
    if "defined(__SWITCH__)" in src:
        info("SDL_egl.h already has a Switch case"); return
    anchor = '#else\n#error "Platform not recognized"'
    if anchor not in src:
        info("SDL_egl.h platform block not in expected form; skipping"); return
    branch = ('#elif defined(__SWITCH__)\n\n'
              'typedef void             *EGLNativeDisplayType;\n'
              'typedef khronos_uintptr_t EGLNativePixmapType;\n'
              'typedef khronos_uintptr_t EGLNativeWindowType;\n\n'
              '#else\n#error "Platform not recognized"')
    egl.write_text(src.replace(anchor, branch, 1))
    info("SDL_egl.h: added Switch EGL native types (fixes Taisei gles.c #error)")


SWITCH_AUDOUT_H = r'''/*
  Simple DirectMedia Layer
  Copyright (C) 1997-2026 Sam Lantinga <slouken@libsdl.org>

  This software is provided 'as-is', without any express or implied
  warranty.  In no event will the authors be held liable for any damages
  arising from the use of this software.

  Permission is granted to anyone to use this software for any purpose,
  including commercial applications, and to alter it and redistribute it
  freely, subject to the following restrictions:

  1. The origin of this software must not be misrepresented; you must not
     claim that you wrote the original software. If you use this software
     in a product, an acknowledgment in the product documentation would be
     appreciated but is not required.
  2. Altered source versions must be plainly marked as such, and must not be
     misrepresented as being the original software.
  3. This notice may not be removed or altered from any source distribution.
*/
#ifndef SDL_switchaudio_h_
#define SDL_switchaudio_h_

#include "SDL_internal.h"
#include "../SDL_sysaudio.h"

#include <switch.h>

#define NUM_AUDIO_BUFFERS 4

struct SDL_PrivateAudioData
{
    Uint8 *mixbuf;                            // buffer SDL fills each iteration
    AudioOutBuffer buffers[NUM_AUDIO_BUFFERS]; // libnx AUDOUT buffer pool
    int next;                                  // next pool slot to submit
    int queued;                                // buffers submitted to AUDOUT, not yet released
};

#endif // SDL_switchaudio_h_
'''

SWITCH_AUDOUT_C = r'''/*
  Simple DirectMedia Layer
  Copyright (C) 1997-2026 Sam Lantinga <slouken@libsdl.org>

  This software is provided 'as-is', without any express or implied
  warranty.  In no event will the authors be held liable for any damages
  arising from the use of this software.

  Permission is granted to anyone to use this software for any purpose,
  including commercial applications, and to alter it and redistribute it
  freely, subject to the following restrictions:

  1. The origin of this software must not be misrepresented; you must not
     claim that you wrote the original software. If you use this software
     in a product, an acknowledgment in the product documentation would be
     appreciated but is not required.
  2. Altered source versions must be plainly marked as such, and must not be
     misrepresented as being the original software.
  3. This notice may not be removed or altered from any source distribution.
*/
#include "SDL_internal.h"

#ifdef SDL_AUDIO_DRIVER_SWITCH

#include "../SDL_sysaudio.h"
#include "SDL_switchaudio.h"

#include <malloc.h> // memalign
#include <switch.h>

#define SWITCHAUDIO_DRIVER_NAME "switch"

// libnx AUDOUT requires 0x1000-aligned buffers and sizes.
#define SWITCH_AUDIO_ALIGN 0x1000
#define ALIGN_UP(x, a) (((x) + ((a) - 1)) & ~((u64)(a) - 1))

static bool SWITCHAUDIO_OpenDevice(SDL_AudioDevice *device)
{
    bool audout_inited = false;
    bool audout_started = false;
    u64 aligned;
    int i;

    device->hidden = (struct SDL_PrivateAudioData *)SDL_calloc(1, sizeof(*device->hidden));
    if (!device->hidden) {
        return false;
    }

    // AUDOUT is fixed at 48000 Hz, 2 channels, signed 16-bit little-endian.
    device->spec.freq = 48000;
    device->spec.channels = 2;
    device->spec.format = SDL_AUDIO_S16LE;
    SDL_UpdatedAudioDeviceFormat(device);

    if (R_FAILED(audoutInitialize())) {
        SDL_SetError("audoutInitialize() failed");
        goto failed;
    }
    audout_inited = true;

    if (R_FAILED(audoutStartAudioOut())) {
        SDL_SetError("audoutStartAudioOut() failed");
        goto failed;
    }
    audout_started = true;

    aligned = ALIGN_UP((u64)device->buffer_size, SWITCH_AUDIO_ALIGN);
    for (i = 0; i < NUM_AUDIO_BUFFERS; i++) {
        void *mem = memalign(SWITCH_AUDIO_ALIGN, aligned);
        if (!mem) {
            SDL_SetError("Out of memory allocating AUDOUT buffer");
            goto failed;
        }
        SDL_memset(mem, 0, aligned);
        device->hidden->buffers[i].next = NULL;
        device->hidden->buffers[i].buffer = mem;
        device->hidden->buffers[i].buffer_size = aligned;
        device->hidden->buffers[i].data_size = device->buffer_size;
        device->hidden->buffers[i].data_offset = 0;
    }

    device->hidden->mixbuf = (Uint8 *)SDL_malloc(device->buffer_size);
    if (!device->hidden->mixbuf) {
        goto failed;
    }
    SDL_memset(device->hidden->mixbuf, device->silence_value, device->buffer_size);
    device->hidden->next = 0;
    device->hidden->queued = 0;

    return true;

failed:
    // Unwind any partial initialization so the device can be reopened cleanly.
    for (i = 0; i < NUM_AUDIO_BUFFERS; i++) {
        if (device->hidden->buffers[i].buffer) {
            free(device->hidden->buffers[i].buffer); // memalign() pairs with free()
            device->hidden->buffers[i].buffer = NULL;
        }
    }
    if (device->hidden->mixbuf) {
        SDL_free(device->hidden->mixbuf);
        device->hidden->mixbuf = NULL;
    }
    if (audout_started) {
        audoutStopAudioOut();
    }
    if (audout_inited) {
        audoutExit();
    }
    SDL_free(device->hidden);
    device->hidden = NULL;
    return false;
}

static Uint8 *SWITCHAUDIO_GetDeviceBuf(SDL_AudioDevice *device, int *buffer_size)
{
    (void)buffer_size;
    return device->hidden->mixbuf;
}

static bool SWITCHAUDIO_PlayDevice(SDL_AudioDevice *device, const Uint8 *buffer, int buflen)
{
    AudioOutBuffer *b = &device->hidden->buffers[device->hidden->next];

    SDL_memcpy(b->buffer, buffer, buflen);
    b->data_size = (u64)buflen;
    device->hidden->next = (device->hidden->next + 1) % NUM_AUDIO_BUFFERS;

    if (R_FAILED(audoutAppendAudioOutBuffer(b))) {
        return SDL_SetError("audoutAppendAudioOutBuffer() failed");
    }
    device->hidden->queued++;
    return true;
}

// Reclaim any buffers the hardware has already finished with,
// without blocking. audoutGetReleasedAudioOutBuffer() is a plain
// (non-blocking) IPC call; each successful call reports at most
// one released buffer, so drain in a loop until it reports none.
static void SWITCHAUDIO_DrainReleased(SDL_AudioDevice *device)
{
    AudioOutBuffer *released = NULL;
    u32 released_count = 0;
    for (;;) {
        released_count = 0;
        if (R_FAILED(audoutGetReleasedAudioOutBuffer(&released, &released_count))) {
            break;
        }
        if (!released_count) {
            break;
        }
        if (device->hidden->queued > 0) {
            device->hidden->queued--;
        }
    }
}

static bool SWITCHAUDIO_WaitDevice(SDL_AudioDevice *device)
{
    AudioOutBuffer *released = NULL;
    u32 released_count = 0;

    // Drain what's already finished first, at no cost.
    SWITCHAUDIO_DrainReleased(device);

    // Ring still has room: return immediately (PlayDevice must not
    // block anyway -- see SDL_audio.c). This lets the mixer thread
    // get ahead of playback and build a cushion of queued buffers,
    // instead of re-synchronizing with the hardware after every
    // single buffer (which is what made a frame hitch on the
    // audio thread turn into an audible BGM glitch).
    if (device->hidden->queued < NUM_AUDIO_BUFFERS) {
        return true;
    }

    // Ring is full: block until the hardware frees a slot, but wake up
    // periodically so we can observe a shutdown request. A queued buffer
    // may never be released once playback is stopping at teardown, so an
    // unbounded wait here deadlocks the SDL_WaitThread() join in
    // ClosePhysicalAudioDevice() -- the app then hangs on exit and the
    // system reports "software closed because an error occurred".
    while (!SDL_GetAtomicInt(&device->shutdown)) {
        released = NULL;
        released_count = 0;
        if (R_SUCCEEDED(audoutWaitPlayFinish(&released, &released_count, 100000000ULL))) {
            if (released_count > (u32)device->hidden->queued) {
                device->hidden->queued = 0;
            } else {
                device->hidden->queued -= (int)released_count;
            }
            break;
        }
        // timed out with nothing released: loop and re-check shutdown
    }
    return true;
}

static void SWITCHAUDIO_CloseDevice(SDL_AudioDevice *device)
{
    if (!device->hidden) {
        return;
    }

    audoutStopAudioOut();
    audoutExit();

    for (int i = 0; i < NUM_AUDIO_BUFFERS; i++) {
        if (device->hidden->buffers[i].buffer) {
            free(device->hidden->buffers[i].buffer); // memalign() pairs with free()
            device->hidden->buffers[i].buffer = NULL;
        }
    }
    if (device->hidden->mixbuf) {
        SDL_free(device->hidden->mixbuf);
        device->hidden->mixbuf = NULL;
    }
    SDL_free(device->hidden);
    device->hidden = NULL;
}

static bool SWITCHAUDIO_Init(SDL_AudioDriverImpl *impl)
{
    impl->OpenDevice = SWITCHAUDIO_OpenDevice;
    impl->GetDeviceBuf = SWITCHAUDIO_GetDeviceBuf;
    impl->PlayDevice = SWITCHAUDIO_PlayDevice;
    impl->WaitDevice = SWITCHAUDIO_WaitDevice;
    impl->CloseDevice = SWITCHAUDIO_CloseDevice;
    impl->OnlyHasDefaultPlaybackDevice = true;
    impl->HasRecordingSupport = false;
    return true;
}

AudioBootStrap SWITCHAUDIO_bootstrap = {
    SWITCHAUDIO_DRIVER_NAME,
    "SDL Nintendo Switch (libnx AUDOUT) audio driver",
    SWITCHAUDIO_Init,
    false,
    false
};

#endif // SDL_AUDIO_DRIVER_SWITCH
'''

def use_audout_audio_backend(sdl_root):
    """Replace devkitPro's audren audio backend with the libnx AUDOUT backend.

    devkitPro drives audio through audren (a software mixer whose SDL glue runs
    an extra per-callback update/IPC and waits on its own render frames). On the
    Switch's shared cores that audio-thread work periodically contends with
    Taisei's render thread and shows up as frame dips, most visible in handheld
    mode where the CPU/GPU budget is tightest. AUDOUT instead hands PCM straight
    to the hardware sink with almost no game-side threading.

    We overwrite src/audio/switch/SDL_switchaudio.{c,h} with the AUDOUT backend
    (same file names, same SWITCHAUDIO_bootstrap symbol and "switch" driver
    name, so devkitPro's CMake picks it up with no build changes; audout lives
    in libnx, already linked via -lnx). The embedded backend uses a 4-deep
    buffer ring with a non-blocking drain in WaitDevice() (so the mixer keeps a
    cushion instead of re-synchronizing every buffer -> no BGM glitch), and a
    finite, shutdown-aware wait (so the audio thread can't deadlock the
    shutdown join)."""
    adir = sdl_root / "src/audio/switch"
    if not adir.exists():
        die("SDL audio/switch dir not found; cannot install AUDOUT backend")
    (adir / "SDL_switchaudio.h").write_text(SWITCH_AUDOUT_H)
    (adir / "SDL_switchaudio.c").write_text(SWITCH_AUDOUT_C)
    info("switch audio: replaced audren backend with libnx AUDOUT "
         "(4-buffer cushion + shutdown-aware wait; avoids audren frame dips)")


def fix_wallpaper_draw(taisei_root):
    """The wallpaper quad was being culled. draw_framebuffer_attachment(), which
    renders to the same target, explicitly sets r_cull(CULL_BACK); the wallpaper
    draw did not, so it inherited whatever cull state the 3D/postprocess pass
    left active (often CULL_FRONT), which culled the quad away. Explicitly set
    CULL_BACK for the wallpaper draw, and restore the previous cull mode
    afterward so later draws in the frame are unaffected."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if "r_cull(CULL_BACK)" in t and "CullFaceMode saved_cull" in t:
        info("video.c: wallpaper draw (cull fix) already applied"); return
    old = (
        'static void nx_wallpaper_draw(void) {\n'
        '    if(!nx_wallpaper) {\n'
        '        return;\n'
        '    }\n'
        '    IntExtent scr = video_get_screen_framebuffer_size();\n'
        '    FloatRect vp;\n'
        '    r_framebuffer_viewport_current(NULL, &vp);  // current (pillarbox) viewport\n'
        '    int left_w  = (int)(vp.x + 0.5f);\n'
        '    int right_x = (int)(vp.x + vp.w + 0.5f);\n'
        '    int right_w = scr.w - right_x;\n'
        '    if(left_w <= 0 && right_w <= 0) {\n'
        '        return;  // no bars\n'
        '    }\n'
        '    IntRect saved_scissor;\n'
        '    r_scissor_current(&saved_scissor);\n'
        '    r_framebuffer_viewport(NULL, 0, 0, scr.w, scr.h);  // full screen for the wallpaper\n'
        '    r_mat_mv_push();\n'
        '    r_uniform_sampler("tex", nx_wallpaper);\n'
        '    r_mat_mv_scale(SCREEN_W, SCREEN_H, 1);\n'
        '    r_mat_mv_translate(0.5, 0.5, 0);\n'
        '    if(left_w > 0) {\n'
        '        r_scissor(0, 0, left_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    if(right_w > 0) {\n'
        '        r_scissor(right_x, 0, right_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    r_mat_mv_pop();\n'
        '    r_scissor_rect(saved_scissor);\n'
        '    r_framebuffer_viewport_rect(NULL, vp);  // restore pillarbox viewport for the game\n'
        '}\n'
    )
    new = (
        'static void nx_wallpaper_draw(void) {\n'
        '    if(!nx_wallpaper) {\n'
        '        return;\n'
        '    }\n'
        '    IntExtent scr = video_get_screen_framebuffer_size();\n'
        '    FloatRect vp;\n'
        '    r_framebuffer_viewport_current(NULL, &vp);  // current (pillarbox) viewport\n'
        '    int left_w  = (int)(vp.x + 0.5f);\n'
        '    int right_x = (int)(vp.x + vp.w + 0.5f);\n'
        '    int right_w = scr.w - right_x;\n'
        '    if(left_w <= 0 && right_w <= 0) {\n'
        '        return;  // no bars\n'
        '    }\n'
        '    IntRect saved_scissor;\n'
        '    r_scissor_current(&saved_scissor);\n'
        '    CullFaceMode saved_cull = r_cull_current();\n'
        '    r_framebuffer_viewport(NULL, 0, 0, scr.w, scr.h);  // full screen for the wallpaper\n'
        '    r_cull(CULL_BACK);  // draw_framebuffer_attachment() does this too; without it the quad is culled\n'
        '    r_mat_mv_push();\n'
        '    r_uniform_sampler("tex", nx_wallpaper);\n'
        '    r_mat_mv_scale(SCREEN_W, SCREEN_H, 1);\n'
        '    r_mat_mv_translate(0.5, 0.5, 0);\n'
        '    if(left_w > 0) {\n'
        '        r_scissor(0, 0, left_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    if(right_w > 0) {\n'
        '        r_scissor(right_x, 0, right_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    r_mat_mv_pop();\n'
        '    r_cull(saved_cull);\n'
        '    r_scissor_rect(saved_scissor);\n'
        '    r_framebuffer_viewport_rect(NULL, vp);  // restore pillarbox viewport for the game\n'
        '}\n'
    )
    if old not in t:
        info("video.c: wallpaper draw fn not in expected form; skipping"); return
    t = t.replace(old, new, 1)
    v.write_text(t)
    info("video.c: wallpaper draw -> CULL_BACK (was inheriting undefined cull state)")


def fix_wallpaper_no_postprocess(taisei_root):
    """Root cause of the wallpaper never appearing: nx_wallpaper_draw() was only
    called from inside video_swap_buffers()'s `if(pp_fb)` branch. pp_fb comes from
    video_postprocess_render(video.postprocess), which returns NULL whenever
    video.postprocess itself is NULL -- and video_postprocess_init() returns NULL
    whenever the optional "global" postprocess shader resource isn't loaded. On
    this Switch build that shader isn't present, so video.postprocess is NULL,
    pp_fb is always NULL, and video_swap_buffers() always takes the `else` branch
    (just r_swap(video.window)) -- meaning nx_wallpaper_draw() never ran, ever.

    The fix has two parts:
      1. Make nx_wallpaper_draw() fully self-contained. It used to rely on its
         caller having already bound the screen framebuffer (r_framebuffer(NULL))
         and set up a standard shader + orthographic projection + white color --
         true in the `if(pp_fb)` branch, not true anywhere else. Now it wraps
         itself in r_state_push()/r_state_pop() (which -- per Taisei's own
         renderer/common/state.c -- restores the bound framebuffer, shader, cull
         mode and scissor automatically) and sets its own projection + color, so
         it behaves identically no matter where it's called from.
      2. Call it from the `else` branch too, since that's the branch this build
         actually takes every frame.
    """
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if "Fully self-contained" in t:
        info("video.c: wallpaper no-postprocess fix already applied"); return

    old_fn = (
        'static void nx_wallpaper_draw(void) {\n'
        '    if(!nx_wallpaper) {\n'
        '        return;\n'
        '    }\n'
        '    IntExtent scr = video_get_screen_framebuffer_size();\n'
        '    FloatRect vp;\n'
        '    r_framebuffer_viewport_current(NULL, &vp);  // current (pillarbox) viewport\n'
        '    int left_w  = (int)(vp.x + 0.5f);\n'
        '    int right_x = (int)(vp.x + vp.w + 0.5f);\n'
        '    int right_w = scr.w - right_x;\n'
        '    if(left_w <= 0 && right_w <= 0) {\n'
        '        return;  // no bars\n'
        '    }\n'
        '    IntRect saved_scissor;\n'
        '    r_scissor_current(&saved_scissor);\n'
        '    CullFaceMode saved_cull = r_cull_current();\n'
        '    r_framebuffer_viewport(NULL, 0, 0, scr.w, scr.h);  // full screen for the wallpaper\n'
        '    r_cull(CULL_BACK);  // draw_framebuffer_attachment() does this too; without it the quad is culled\n'
        '    r_mat_mv_push();\n'
        '    r_uniform_sampler("tex", nx_wallpaper);\n'
        '    r_mat_mv_scale(SCREEN_W, SCREEN_H, 1);\n'
        '    r_mat_mv_translate(0.5, 0.5, 0);\n'
        '    if(left_w > 0) {\n'
        '        r_scissor(0, 0, left_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    if(right_w > 0) {\n'
        '        r_scissor(right_x, 0, right_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    r_mat_mv_pop();\n'
        '    r_cull(saved_cull);\n'
        '    r_scissor_rect(saved_scissor);\n'
        '    r_framebuffer_viewport_rect(NULL, vp);  // restore pillarbox viewport for the game\n'
        '}\n'
    )
    new_fn = (
        'static void nx_wallpaper_draw(void) {\n'
        '    if(!nx_wallpaper) {\n'
        '        return;\n'
        '    }\n'
        '    IntExtent scr = video_get_screen_framebuffer_size();\n'
        '    FloatRect vp;\n'
        '    r_framebuffer_viewport_current(NULL, &vp);  // pillarbox viewport (kept on the screen fb)\n'
        '    int left_w  = (int)(vp.x + 0.5f);\n'
        '    int right_x = (int)(vp.x + vp.w + 0.5f);\n'
        '    int right_w = scr.w - right_x;\n'
        '    if(left_w <= 0 && right_w <= 0) {\n'
        '        return;  // no bars\n'
        '    }\n'
        '\n'
        '    // Fully self-contained: state_push/pop restores the bound framebuffer,\n'
        '    // shader, cull mode and scissor for us, so this is safe to call from\n'
        '    // anywhere in the frame -- in particular from the no-postprocess path\n'
        '    // below, where none of that render state has been set up by a caller.\n'
        '    r_state_push();\n'
        '    r_framebuffer(NULL);\n'
        '    r_mat_proj_push_ortho(SCREEN_W, SCREEN_H);\n'
        '    r_shader_standard();\n'
        '    r_color4(1, 1, 1, 1);\n'
        '    r_cull(CULL_BACK);  // draw_framebuffer_attachment() does this too; without it the quad is culled\n'
        '    r_framebuffer_viewport(NULL, 0, 0, scr.w, scr.h);  // full screen for the wallpaper\n'
        '\n'
        '    r_mat_mv_push();\n'
        '    r_uniform_sampler("tex", nx_wallpaper);\n'
        '    r_mat_mv_scale(SCREEN_W, SCREEN_H, 1);\n'
        '    r_mat_mv_translate(0.5, 0.5, 0);\n'
        '    if(left_w > 0) {\n'
        '        r_scissor(0, 0, left_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    if(right_w > 0) {\n'
        '        r_scissor(right_x, 0, right_w, scr.h);\n'
        '        r_draw_quad();\n'
        '    }\n'
        '    r_mat_mv_pop();\n'
        '\n'
        '    r_mat_proj_pop();\n'
        '    r_state_pop();  // restores framebuffer, shader, cull mode, scissor\n'
        '\n'
        '    r_framebuffer_viewport_rect(NULL, vp);  // restore pillarbox viewport for the game\n'
        '}\n'
    )
    if old_fn not in t:
        info("video.c: wallpaper draw fn not in expected form; skipping no-pp fix"); return
    t = t.replace(old_fn, new_fn, 1)

    old_else = '\t} else {\n\t\tr_swap(video.window);\n\t}\n'
    if t.count(old_else) != 1:
        info("video.c: video_swap_buffers else-branch not in expected form; skipping no-pp fix"); return
    new_else = '\t} else {\n#ifdef __SWITCH__\n\t\tnx_wallpaper_draw();\n#endif\n\t\tr_swap(video.window);\n\t}\n'
    t = t.replace(old_else, new_else, 1)

    v.write_text(t)
    info("video.c: wallpaper now also drawn on the no-postprocess path (the one this build actually takes)")


def fix_wallpaper_pixel_format(taisei_root):
    """Root cause of the corrupted/noisy wallpaper: pixmap_load_stream() with
    preferred_format=PIXMAP_FORMAT_RGBA8 only actually returns RGBA8 (4 bytes/px)
    if the source PNG has an alpha channel -- a plain opaque wallpaper.png decodes
    to RGB8 (3 bytes/px) instead. gl33_texture_set() uploads using the *texture's*
    declared transfer format (GL_RGBA/GL_UNSIGNED_BYTE), not the pixmap's actual
    format, guarded only by an assert() that's compiled out in a release build --
    so the 3-vs-4-bytes-per-pixel mismatch desyncs the row stride every scanline
    (the noise) and runs off the end of the buffer near the bottom (the black
    bar). Fix: query the texture type's optimal pixel format and convert the
    pixmap into it before creating/filling the texture -- the same pattern
    Taisei's own texture_loader_prepare_pixmaps() uses for every other texture."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if "TextureTypeQueryResult wp_qr" in t:
        info("video.c: wallpaper pixel-format fix already applied"); return
    old = (
        '    nx_wallpaper = r_texture_create(&(TextureParams) {\n'
        '        .width = px.width,\n'
        '        .height = px.height,\n'
        '        .type = TEX_TYPE_RGBA_8,\n'
        '        .filter = { TEX_FILTER_LINEAR, TEX_FILTER_LINEAR },\n'
        '        .wrap = { TEX_WRAP_CLAMP, TEX_WRAP_CLAMP },\n'
        '        .mipmaps = 1,\n'
        '    });\n'
        '    r_texture_fill(nx_wallpaper, 0, 0, &px);\n'
        '    log_info("Loaded Switch wallpaper %s (%ux%u)", path, px.width, px.height);\n'
        '    mem_free(px.data.untyped);\n'
    )
    new = (
        '    TextureTypeQueryResult wp_qr;\n'
        '    if(!r_texture_type_query(TEX_TYPE_RGBA_8, 0, px.format, &wp_qr)) {\n'
        '        log_error("Switch wallpaper: pixel format not supported by the renderer");\n'
        '        mem_free(px.data.untyped);\n'
        '        return;\n'
        '    }\n'
        '    // A source PNG without an alpha channel decodes to RGB8 (3 bytes/px), not\n'
        '    // RGBA8, regardless of the preferred_format we asked for. Convert into\n'
        '    // whatever format the texture actually expects before filling it, or the\n'
        '    // upload desyncs its row stride (diagonal noise + black bar at the bottom).\n'
        '    pixmap_convert_inplace_realloc(&px, wp_qr.optimal_pixmap_format);\n'
        '\n'
        '    nx_wallpaper = r_texture_create(&(TextureParams) {\n'
        '        .width = px.width,\n'
        '        .height = px.height,\n'
        '        .type = TEX_TYPE_RGBA_8,\n'
        '        .filter = { TEX_FILTER_LINEAR, TEX_FILTER_LINEAR },\n'
        '        .wrap = { TEX_WRAP_CLAMP, TEX_WRAP_CLAMP },\n'
        '        .mipmaps = 1,\n'
        '    });\n'
        '    r_texture_fill(nx_wallpaper, 0, 0, &px);\n'
        '    log_info("Loaded Switch wallpaper %s (%ux%u)", path, px.width, px.height);\n'
        '    mem_free(px.data.untyped);\n'
    )
    if old not in t:
        info("video.c: wallpaper texture-create block not in expected form; skipping pixel-format fix"); return
    t = t.replace(old, new, 1)
    v.write_text(t)
    info("video.c: wallpaper loader -> convert pixmap to the texture's optimal format before upload")


def fix_wallpaper_blend(taisei_root):
    """nx_wallpaper_draw() saves and restores shader, color, cull mode,
    framebuffer, and scissor state via r_state_push()/r_state_pop(), but per
    Taisei's renderer/common/state.c, that stack only restores whatever was
    actually *touched* between push and pop -- a dirty-bit system, not a full
    snapshot. The draw never calls r_blend(), so it runs with whatever blend
    mode the previous draw call left active (typically BLEND_ALPHA, set
    elsewhere in Taisei's rendering). If wallpaper.png has any alpha channel
    data -- transparent padding around the art, a dithered pattern, anti-
    aliased edges -- those pixels blend toward the black clear color instead
    of showing their actual RGB, which appears as missing pixels and black
    patches in the rendered image. Force BLEND_NONE so the wallpaper always
    renders fully opaque."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if "r_blend(BLEND_NONE)" in t:
        info("video.c: wallpaper opaque-blend fix already applied"); return
    old = (
        '    r_color4(1, 1, 1, 1);\n'
        '    r_cull(CULL_BACK);  // draw_framebuffer_attachment() does this too; without it the quad is culled\n'
    )
    new = (
        '    r_color4(1, 1, 1, 1);\n'
        '    r_blend(BLEND_NONE);  // draw fully opaque -- ignore the wallpaper PNG\'s alpha channel\n'
        '    // and whatever blend mode was left active by the previous draw call this frame;\n'
        '    // otherwise partially-transparent pixels in the source image (edge padding,\n'
        '    // dithering, AA) blend toward the black clear color, which looks like missing\n'
        '    // pixels and a black region wherever the source alpha is low.\n'
        '    r_cull(CULL_BACK);  // draw_framebuffer_attachment() does this too; without it the quad is culled\n'
    )
    if old not in t:
        info("video.c: wallpaper draw blend-state site not in expected form; skipping"); return
    t = t.replace(old, new, 1)
    v.write_text(t)
    info("video.c: wallpaper draw -> forced BLEND_NONE (was inheriting undefined blend state)")


def fix_wallpaper_frame_start(taisei_root):
    """Moves the wallpaper draw to the start of the frame instead of the end.
    nx_wallpaper_draw() was previously called at the very end of the frame,
    inside video_swap_buffers() right before the buffer flip -- meaning it
    painted an opaque quad over the pillarbox bars after the game, UI, and
    menus had already been drawn to the screen framebuffer. That ordering is
    wrong for a background layer: any UI element that extends into the bars
    (the settings menu, for example) gets silently painted over and hidden.

    Taisei's actual per-frame entry point is run_render_frame() in
    src/eventloop/eventloop.c: it clears the screen to black, then calls
    frame->render() to draw that frame's content. Moving the wallpaper draw
    to right after that clear -- before frame->render() -- makes it a
    genuine background: everything drawn afterward layers on top of it
    normally, so it can never occlude UI content regardless of what draws
    where. This also replaces the two separate call sites inside
    video_swap_buffers() (one per postprocess / no-postprocess branch) with
    a single unconditional call, once per frame, before any content exists
    to hide."""
    v = taisei_root / "src/video.c"
    h = taisei_root / "src/video.h"
    e = taisei_root / "src/eventloop/eventloop.c"
    if not (v.exists() and h.exists() and e.exists()):
        return

    et = e.read_text()
    if "nx_wallpaper_draw();" in et:
        info("eventloop.c: wallpaper frame-start fix already applied"); return

    vt = v.read_text()
    if "static void nx_wallpaper_draw(void) {" not in vt and "void nx_wallpaper_draw(void) {" not in vt:
        info("video.c: nx_wallpaper_draw not found; skipping frame-start fix"); return

    # 1. video.c: drop `static` so eventloop.c can call it, and remove the two
    #    end-of-frame call sites inside video_swap_buffers().
    vt = vt.replace("static void nx_wallpaper_draw(void) {", "void nx_wallpaper_draw(void) {", 1)

    old_comment = (
        '    // Fully self-contained: state_push/pop restores the bound framebuffer,\n'
        '    // shader, cull mode and scissor for us, so this is safe to call from\n'
        '    // anywhere in the frame -- in particular from the no-postprocess path\n'
        '    // below, where none of that render state has been set up by a caller.\n'
    )
    new_comment = (
        '    // Fully self-contained: state_push/pop restores the bound framebuffer,\n'
        '    // shader, cull mode and scissor for us. Called once from eventloop.c\'s\n'
        '    // run_render_frame(), right after the per-frame clear and before any game\n'
        '    // or UI content is drawn -- so this is a true background layer: nothing\n'
        '    // drawn later in the frame can be occluded by it.\n'
    )
    if old_comment in vt:
        vt = vt.replace(old_comment, new_comment, 1)

    old_pp_call = (
        '\t\tr_color3(1, 1, 1);\n'
        '#ifdef __SWITCH__\n'
        '\t\tnx_wallpaper_draw();\n'
        '#endif\n'
        '\t\tdraw_framebuffer_tex(pp_fb, SCREEN_W, SCREEN_H);\n'
    )
    new_pp_call = (
        '\t\tr_color3(1, 1, 1);\n'
        '\t\tdraw_framebuffer_tex(pp_fb, SCREEN_W, SCREEN_H);\n'
    )
    if old_pp_call in vt:
        vt = vt.replace(old_pp_call, new_pp_call, 1)
    else:
        info("video.c: pp_fb wallpaper call site not in expected form; skipping frame-start fix"); return

    old_else_call = '\t} else {\n#ifdef __SWITCH__\n\t\tnx_wallpaper_draw();\n#endif\n\t\tr_swap(video.window);\n\t}\n'
    new_else_call = '\t} else {\n\t\tr_swap(video.window);\n\t}\n'
    if old_else_call in vt:
        vt = vt.replace(old_else_call, new_else_call, 1)
    else:
        info("video.c: else-branch wallpaper call site not in expected form; skipping frame-start fix"); return

    v.write_text(vt)

    # 2. video.h: declare it so eventloop.c (a different translation unit) can
    #    call it now that it's no longer static.
    ht = h.read_text()
    anchor = '#define SCREEN_SIZE { SCREEN_W, SCREEN_H }\n'
    if anchor not in ht:
        die("video.h: SCREEN_SIZE anchor not found")
    ht = ht.replace(
        anchor,
        anchor + '\n#ifdef __SWITCH__\nvoid nx_wallpaper_draw(void);\n#endif\n',
        1,
    )
    h.write_text(ht)

    # 3. eventloop.c: call it right after the per-frame clear, before any game
    #    or UI content exists -- so it's a background, not an overpaint.
    old_rrf = (
        '\tr_framebuffer_clear(NULL, BUFFER_ALL, RGBA(0, 0, 0, 1), 1);\n'
        '\tRenderFrameAction a = frame->render(frame->context);\n'
    )
    new_rrf = (
        '\tr_framebuffer_clear(NULL, BUFFER_ALL, RGBA(0, 0, 0, 1), 1);\n'
        '#ifdef __SWITCH__\n'
        '\tnx_wallpaper_draw();\n'
        '#endif\n'
        '\tRenderFrameAction a = frame->render(frame->context);\n'
    )
    if old_rrf not in et:
        die("eventloop.c: run_render_frame clear/render site not in expected form")
    et = et.replace(old_rrf, new_rrf, 1)
    e.write_text(et)

    info("eventloop.c: wallpaper now drawn once at frame start (true background, can't occlude UI/text)")


def fix_wallpaper_v2(taisei_root):
    """Reads wallpaper.png with plain fopen/fread -- the same newlib path the
    VFS already uses for data/ -- instead of SDL_IOFromFile, which does not
    reliably read this file on the Switch."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if 'FILE *wp_f = fopen(path, "rb")' in t:
        info("video.c: wallpaper v2 loader already applied"); return
    old = (
        '    char path[1024];\n'
        '    snprintf(path, sizeof(path), "%s/wallpaper.png", nxGetProgramDir());\n'
        '    // wallpaper.png lives next to the .nro (the program dir), which is not a\n'
        '    // VFS mount, so read it straight from the filesystem, not via vfs_open().\n'
        '    SDL_IOStream *wp_io = SDL_IOFromFile(path, "rb");\n'
        '    if(!wp_io) {\n'
        '        return;  // no wallpaper provided; keep plain black bars\n'
        '    }\n'
        '    Pixmap px = { 0 };\n'
        '    bool wp_ok = pixmap_load_stream(wp_io, PIXMAP_FILEFORMAT_AUTO, &px, PIXMAP_FORMAT_RGBA8);\n'
        '    SDL_CloseIO(wp_io);\n'
        '    if(!wp_ok) {\n'
        '        return;  // not a readable image\n'
        '    }\n'
    )
    new = (
        '    char path[1024];\n'
        '    snprintf(path, sizeof(path), "%s/wallpaper.png", nxGetProgramDir());\n'
        '    // Read straight from the SD card with newlib fopen (the exact path the\n'
        '    // VFS already opens data/ with); SDL_IOFromFile proved unreliable here.\n'
        '    FILE *wp_f = fopen(path, "rb");\n'
        '    if(!wp_f) {\n'
        '        return;  // no wallpaper at this path\n'
        '    }\n'
        '    fseek(wp_f, 0, SEEK_END);\n'
        '    long wp_sz = ftell(wp_f);\n'
        '    fseek(wp_f, 0, SEEK_SET);\n'
        '    void *wp_buf = SDL_malloc((size_t)(wp_sz > 0 ? wp_sz : 1));\n'
        '    size_t wp_rd = fread(wp_buf, 1, (size_t)(wp_sz > 0 ? wp_sz : 0), wp_f);\n'
        '    fclose(wp_f);\n'
        '    SDL_IOStream *wp_io = SDL_IOFromConstMem(wp_buf, wp_rd);\n'
        '    Pixmap px = { 0 };\n'
        '    bool wp_ok = wp_io && pixmap_load_stream(wp_io, PIXMAP_FILEFORMAT_AUTO, &px, PIXMAP_FORMAT_RGBA8);\n'
        '    if(wp_io) { SDL_CloseIO(wp_io); }\n'
        '    SDL_free(wp_buf);\n'
        '    if(!wp_ok) {\n'
        '        return;  // not a readable image\n'
        '    }\n'
    )
    if old not in t:
        info("video.c: v1 wallpaper loader not found; skipping v2"); return
    t = t.replace(old, new, 1)
    v.write_text(t)
    info("video.c: wallpaper loader -> fopen/fread (avoids SDL_IOFromFile)")


def fix_wallpaper_loader(taisei_root):
    """wallpaper.png sits next to the .nro (the program dir), which is NOT a VFS
    mount -- so loading it via pixmap_load_file() (which uses vfs_open) silently
    fails and no wallpaper appears. Read it straight from the filesystem with
    SDL_IOFromFile() instead (same POSIX path the program-dir data already uses)."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        return
    t = v.read_text()
    if "SDL_IOFromFile(path" in t:
        info("video.c: wallpaper loader already reads from the filesystem"); return
    old = (
        '    char path[1024];\n'
        '    snprintf(path, sizeof(path), "%s/wallpaper.png", nxGetProgramDir());\n'
        '    Pixmap px = { 0 };\n'
        '    if(!pixmap_load_file(path, &px, PIXMAP_FORMAT_RGBA8)) {\n'
        '        return;  // no wallpaper provided; keep plain black bars\n'
        '    }\n'
    )
    new = (
        '    char path[1024];\n'
        '    snprintf(path, sizeof(path), "%s/wallpaper.png", nxGetProgramDir());\n'
        '    // wallpaper.png lives next to the .nro (the program dir), which is not a\n'
        '    // VFS mount, so read it straight from the filesystem, not via vfs_open().\n'
        '    SDL_IOStream *wp_io = SDL_IOFromFile(path, "rb");\n'
        '    if(!wp_io) {\n'
        '        return;  // no wallpaper provided; keep plain black bars\n'
        '    }\n'
        '    Pixmap px = { 0 };\n'
        '    bool wp_ok = pixmap_load_stream(wp_io, PIXMAP_FILEFORMAT_AUTO, &px, PIXMAP_FORMAT_RGBA8);\n'
        '    SDL_CloseIO(wp_io);\n'
        '    if(!wp_ok) {\n'
        '        return;  // not a readable image\n'
        '    }\n'
    )
    if old not in t:
        info("video.c: wallpaper load block not in expected form; skipping"); return
    t = t.replace(old, new, 1)
    v.write_text(t)
    info("video.c: wallpaper now loaded from the filesystem (SDL_IOFromFile)")


def add_switch_wallpaper(taisei_root):
    """Optional user wallpaper drawn into the Switch pillarbox bars only. Put a
    wallpaper.png (1080p ideal) next to the .nro at /switch/taisei/wallpaper.png;
    its left/right edges fill the black bars. Absent -> unchanged. Scissored to
    the bars, so nothing draws under the game (no overdraw / perf cost)."""
    v = taisei_root / "src/video.c"
    if not v.exists():
        info("video.c not found; skipping wallpaper feature"); return
    t = v.read_text()
    if "nx_wallpaper" in t:
        info("video.c: wallpaper feature already present"); return

    block = '#include "video_postprocess.h"\n' + """
#ifdef __SWITCH__
#include "arch_switch.h"
#include "pixmap/pixmap.h"

static IntExtent video_get_screen_framebuffer_size(void);

static Texture *nx_wallpaper;

// Optional user wallpaper next to the .nro (/switch/taisei/wallpaper.png).
static void nx_wallpaper_load(void) {
    char path[1024];
    snprintf(path, sizeof(path), "%s/wallpaper.png", nxGetProgramDir());
    Pixmap px = { 0 };
    if(!pixmap_load_file(path, &px, PIXMAP_FORMAT_RGBA8)) {
        return;  // no wallpaper provided; keep plain black bars
    }
    nx_wallpaper = r_texture_create(&(TextureParams) {
        .width = px.width,
        .height = px.height,
        .type = TEX_TYPE_RGBA_8,
        .filter = { TEX_FILTER_LINEAR, TEX_FILTER_LINEAR },
        .wrap = { TEX_WRAP_CLAMP, TEX_WRAP_CLAMP },
        .mipmaps = 1,
    });
    r_texture_fill(nx_wallpaper, 0, 0, &px);
    log_info("Loaded Switch wallpaper %s (%ux%u)", path, px.width, px.height);
    mem_free(px.data.untyped);
}

// Draw the wallpaper into the left/right bars only (mapped 1:1 to the screen).
static void nx_wallpaper_draw(void) {
    if(!nx_wallpaper) {
        return;
    }
    IntExtent scr = video_get_screen_framebuffer_size();
    FloatRect vp;
    r_framebuffer_viewport_current(NULL, &vp);  // current (pillarbox) viewport
    int left_w  = (int)(vp.x + 0.5f);
    int right_x = (int)(vp.x + vp.w + 0.5f);
    int right_w = scr.w - right_x;
    if(left_w <= 0 && right_w <= 0) {
        return;  // no bars
    }
    IntRect saved_scissor;
    r_scissor_current(&saved_scissor);
    r_framebuffer_viewport(NULL, 0, 0, scr.w, scr.h);  // full screen for the wallpaper
    r_mat_mv_push();
    r_uniform_sampler("tex", nx_wallpaper);
    r_mat_mv_scale(SCREEN_W, SCREEN_H, 1);
    r_mat_mv_translate(0.5, 0.5, 0);
    if(left_w > 0) {
        r_scissor(0, 0, left_w, scr.h);
        r_draw_quad();
    }
    if(right_w > 0) {
        r_scissor(right_x, 0, right_w, scr.h);
        r_draw_quad();
    }
    r_mat_mv_pop();
    r_scissor_rect(saved_scissor);
    r_framebuffer_viewport_rect(NULL, vp);  // restore pillarbox viewport for the game
}
#endif
"""
    if '#include "video_postprocess.h"\n' not in t:
        die("video.c: include anchor not found")
    t = t.replace('#include "video_postprocess.h"\n', block, 1)

    old_pi = '\tvideo.postprocess = video_postprocess_init();\n\tr_framebuffer(video_get_screen_framebuffer());\n}'
    new_pi = '\tvideo.postprocess = video_postprocess_init();\n\tr_framebuffer(video_get_screen_framebuffer());\n#ifdef __SWITCH__\n\tnx_wallpaper_load();\n#endif\n}'
    if old_pi not in t:
        die("video.c: video_post_init tail not in expected form")
    t = t.replace(old_pi, new_pi, 1)

    old_sw = '\t\tr_color3(1, 1, 1);\n\t\tdraw_framebuffer_tex(pp_fb, SCREEN_W, SCREEN_H);\n'
    new_sw = '\t\tr_color3(1, 1, 1);\n#ifdef __SWITCH__\n\t\tnx_wallpaper_draw();\n#endif\n\t\tdraw_framebuffer_tex(pp_fb, SCREEN_W, SCREEN_H);\n'
    if old_sw not in t:
        die("video.c: video_swap_buffers blit site not in expected form")
    t = t.replace(old_sw, new_sw, 1)

    old_sd = 'void video_shutdown(void) {\n\tvideo_postprocess_shutdown(video.postprocess);\n'
    new_sd = 'void video_shutdown(void) {\n\tvideo_postprocess_shutdown(video.postprocess);\n#ifdef __SWITCH__\n\tif(nx_wallpaper) {\n\t\tr_texture_destroy(nx_wallpaper);\n\t\tnx_wallpaper = NULL;\n\t}\n#endif\n'
    if old_sd not in t:
        die("video.c: video_shutdown head not in expected form")
    t = t.replace(old_sd, new_sd, 1)

    v.write_text(t)
    info("video.c: added optional Switch wallpaper (drawn in the pillarbox bars)")


def patch_taisei_switch_source(taisei_root):
    """Fix bitrot in Taisei v1.4.5's (untested) Switch source paths:
       * src/vfs/setup_switch.c uses strfmt(), removed from Taisei -> add a local one.
       * src/vfs/syspath_posix.c unconditionally uses <sys/mman.h>/mmap, absent on
         devkitA64 -> guard it (the VFS already null-checks .mmap, nodeapi.c)."""
    # --- setup_switch.c: provide a local strfmt() ---------------------------
    setup = taisei_root / "src/vfs/setup_switch.c"
    if setup.exists():
        t = setup.read_text()
        if "static char *strfmt(" not in t:
            anchor = '#include "util/stringops.h"\n'
            helper = anchor + (
                "\n#include <stdarg.h>\n#include <stdio.h>\n#include <stdlib.h>\n\n"
                "// strfmt() was removed from Taisei after the Switch VFS backend\n"
                "// bitrotted; provide a local malloc-based equivalent (freed with\n"
                "// free() below, as the original did).\n"
                "static char *strfmt(const char *fmt, ...) {\n"
                "\tva_list ap, ap2;\n"
                "\tva_start(ap, fmt);\n"
                "\tva_copy(ap2, ap);\n"
                "\tint n = vsnprintf(NULL, 0, fmt, ap);\n"
                "\tva_end(ap);\n"
                "\tchar *out = (char *)malloc((size_t)n + 1);\n"
                "\tif(out) {\n"
                "\t\tvsnprintf(out, (size_t)n + 1, fmt, ap2);\n"
                "\t}\n"
                "\tva_end(ap2);\n"
                "\treturn out;\n"
                "}\n"
            )
            if anchor not in t:
                die("setup_switch.c: expected include anchor not found")
            setup.write_text(t.replace(anchor, helper, 1))
            info("setup_switch.c: added local strfmt()")
        else:
            info("setup_switch.c: strfmt already present")

    # --- syspath_posix.c: guard mmap behind __has_include(<sys/mman.h>) ------
    sp = taisei_root / "src/vfs/syspath_posix.c"
    if sp.exists():
        t = sp.read_text()
        if "TAISEI_HAVE_SYS_MMAN" not in t:
            t = t.replace(
                "#include <sys/mman.h>\n",
                "#if defined(__has_include)\n"
                "#  if __has_include(<sys/mman.h>)\n"
                "#    define TAISEI_HAVE_SYS_MMAN 1\n"
                "#  endif\n"
                "#endif\n"
                "#ifdef TAISEI_HAVE_SYS_MMAN\n"
                "#include <sys/mman.h>\n"
                "#endif\n", 1)
            t = t.replace(
                "static const void *vfs_syspath_mmap(VFSNode *node, size_t *size) {",
                "#ifdef TAISEI_HAVE_SYS_MMAN\n"
                "static const void *vfs_syspath_mmap(VFSNode *node, size_t *size) {", 1)
            munmap_block = (
                "static bool vfs_syspath_munmap(VFSNode *node, const void *addr, size_t size) {\n"
                "\tif(munmap((void*)addr, size) < 0) {\n"
                "\t\tvfs_set_error(\"munmap() failed: %s\", strerror(errno));\n"
                "\t\treturn false;\n"
                "\t}\n\n"
                "\treturn true;\n"
                "}\n"
            )
            if munmap_block not in t:
                die("syspath_posix.c: munmap block not in expected form")
            t = t.replace(munmap_block, munmap_block + "#endif\n", 1)
            vtable = "\t.mmap = vfs_syspath_mmap,\n\t.munmap = vfs_syspath_munmap,\n"
            if vtable not in t:
                die("syspath_posix.c: mmap vtable entries not in expected form")
            t = t.replace(vtable, "#ifdef TAISEI_HAVE_SYS_MMAN\n" + vtable + "#endif\n", 1)
            sp.write_text(t)
            info("syspath_posix.c: guarded mmap for platforms without <sys/mman.h>")
        else:
            info("syspath_posix.c: mmap already guarded")

    # --- switch/crossfile.sh: put mesa EGL on Taisei's own link line ---------
    # SDL3's static switch video backend references eglGetDisplay/etc; those live
    # in mesa's libEGL. Add EGL + its static deps to the crossfile link flags so
    # the final taisei.elf link resolves them.
    cf = taisei_root / "switch/crossfile.sh"
    if cf.exists():
        t = cf.read_text()
        if "-lEGL" not in t:
            old = 'ADDITIONAL_LINK_FLAGS="-specs=$DEVKITPRO/libnx/switch.specs"'
            new = 'ADDITIONAL_LINK_FLAGS="-specs=$DEVKITPRO/libnx/switch.specs -lEGL -lstdc++ -lglapi -ldrm_nouveau"'
            if old not in t:
                die("switch/crossfile.sh: ADDITIONAL_LINK_FLAGS line not found")
            cf.write_text(t.replace(old, new, 1))
            info("switch/crossfile.sh: added mesa EGL to link flags")
        else:
            info("switch/crossfile.sh: EGL link flags already present")


def ensure_pack_deps():
    """Taisei's asset packer (scripts/pack.py) needs zstd-in-zip: Python 3.14's
    compression.zstd, or the 'backports.zstd' package on older Pythons. Make sure
    whatever python3 meson/ninja will invoke has it."""
    stage("ensuring asset packer zstd support (backports.zstd)")
    check = (
        "try:\n"
        "    from compression.zstd import ZstdCompressor\n"
        "    from zipfile import ZIP_ZSTANDARD\n"
        "except Exception:\n"
        "    from backports.zstd import ZstdCompressor\n"
        "    from backports.zstd.zipfile import ZIP_ZSTANDARD\n"
    )
    seen = []
    for py in (sys.executable, shutil.which("python3"), shutil.which("python")):
        if py and py not in seen:
            seen.append(py)
    for py in seen:
        if subprocess.run([py, "-c", check], capture_output=True).returncode == 0:
            info(f"{py}: OK"); continue
        info(f"{py}: installing backports.zstd")
        try:
            run([py, "-m", "pip", "install", "--quiet", "backports.zstd"])
        except subprocess.CalledProcessError:
            run([py, "-m", "pip", "install", "--quiet", "--user", "backports.zstd"])


def fix_nacp_version(taisei_root):
    """The NACP version (what hbmenu shows) comes from meson.project_version(),
    computed at configure time from `git describe --dirty`. Even with a .VERSION
    override it can stay 'X.Y.Z-dirty-HEAD' across incremental reconfigures, and
    it's a separate path from the in-game label (version_auto.c, regenerated every
    build). taisei_version_string feeds ONLY the NACP, so strip the suffix right at
    its definition -> hbmenu shows a clean 'X.Y.Z' no matter what git-describe says."""
    mb = taisei_root / "meson.build"
    if not mb.exists():
        info("meson.build not found; skipping NACP version fix"); return
    t = mb.read_text()
    new = "taisei_version_string = meson.project_version().split('-')[0]\n"
    if new in t:
        info("meson.build: NACP version already stripped"); return
    old = "taisei_version_string = meson.project_version()\n"
    if old not in t:
        info("meson.build: taisei_version_string line not in expected form; skipping"); return
    mb.write_text(t.replace(old, new, 1))
    info("meson.build: NACP version stripped to clean 'X.Y.Z' for hbmenu")


def write_clean_version(taisei_root):
    """version.py runs `git describe --dirty`; since this script modifies the
    source tree before building, that yields e.g. 'v1.4.5-dirty-<branch>'
    instead of a clean version string. A .VERSION override file (checked
    before git) pins a clean label."""
    vf = taisei_root / ".VERSION"
    want = TAISEI_TAG  # e.g. "v1.4.5"
    if vf.exists() and vf.read_text().strip() == want:
        info(".VERSION already clean"); return
    vf.write_text(want + "\n")
    info(f".VERSION set to {want} (clean version label, no -dirty suffix)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="taisei-switch-build")
    ap.add_argument("--devkitpro", default=os.environ.get("DEVKITPRO", "/opt/devkitpro"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--clean", action="store_true", help="wipe workdir first")
    args = ap.parse_args()

    dkp = os.path.abspath(args.devkitpro)
    if not Path(dkp, "switchvars.sh").exists():
        die(f"devkitPro not found at {dkp} (no switchvars.sh). Install it or pass --devkitpro.")
    for tool in ("git", "cmake", "meson", "ninja"):
        if not shutil.which(tool):
            die(f"'{tool}' not found in PATH. Install it (e.g. pip install meson ninja; brew install cmake).")

    work = Path(args.workdir).resolve()
    if args.clean and work.exists():
        stage(f"cleaning {work}"); shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    info(f"workdir: {work}")
    info(f"DEVKITPRO: {dkp}")

    portlibs = bash_capture('echo "$PORTLIBS_PREFIX"', dkp)
    info(f"portlibs prefix: {portlibs}")

    # -- 1  devkitPro SDL3 (full libnx backend; no third-party backend patch) --
    sdl = work / "SDL"
    if not sdl.exists():
        stage("cloning devkitPro SDL3 (switch-sdl-3.4, SDL 3.4.0)")
        run(["git", "clone", "--depth", "1", "-b", SDL_BRANCH, SDL_REPO, str(sdl)])
    else:
        info("devkitPro SDL tree already present")

    video_c = sdl / "src/video/switch/SDL_switchvideo.c"
    if not video_c.exists():
        die("devkitPro SDL tree is missing src/video/switch/SDL_switchvideo.c "
            "(wrong branch? expected switch-sdl-3.4)")
    # devkitPro's Switch backend is used verbatim -- it already provides:
    #   * GLES3 via SDL's core EGL path (honors Taisei's SDL_GL_SetAttribute)
    #   * a clean, non-blocking audio-device teardown (WaitDevice() returns
    #     promptly, so no shutdown patch is needed)
    #   * Nintendo-swapped A/B face-button mapping
    #   * `sdl_link_dependency(opengl LIBS EGL stdc++ glapi drm_nouveau)` in its
    #     CMakeLists, which exports the mesa EGL deps through sdl3.pc so Taisei
    #     resolves eglGetDisplay/etc automatically.
    info("using devkitPro's native Switch backend as-is")
    # The single SDL-side patch still required: give SDL_egl.h a __SWITCH__ case
    # so Taisei's gles.c doesn't hit its "Platform not recognized" #error.
    stage("patching SDL_egl.h for the Switch (native EGL types)")
    patch_sdl_egl_switch(sdl)
    # Replace audren with the libnx AUDOUT backend. audren's audio-thread work
    # (per-callback update + IPC + render-frame waits) contends with Taisei's
    # render thread on the Switch's shared cores and causes frame dips, most
    # visible in handheld mode where the CPU/GPU budget is tightest. AUDOUT
    # hands PCM straight to the hardware sink instead.
    stage("switching switch audio backend audren -> AUDOUT")
    use_audout_audio_backend(sdl)

    # -- 2  build + install SDL3 into portlibs --------------------------------
    stage("building SDL3 (static) for Switch and installing into portlibs")
    install_cmd = ("cmake --install build-switch"
                   if os.access(portlibs, os.W_OK)
                   else "sudo cmake --install build-switch")
    if not os.access(portlibs, os.W_OK):
        info("portlibs is not writable by you; the install step will use sudo (you may be prompted).")
    bash(textwrap.dedent(f"""
        cmake -S "{sdl}" -B "{sdl}/build-switch" \\
          -DCMAKE_TOOLCHAIN_FILE="$DEVKITPRO/cmake/Switch.cmake" \\
          -DCMAKE_SYSTEM_NAME=NintendoSwitch \\
          -DCMAKE_BUILD_TYPE=Release \\
          -DCMAKE_INSTALL_PREFIX="$PORTLIBS_PREFIX" \\
          -DSDL_STATIC=ON -DSDL_SHARED=OFF \\
          -DSDL_TESTS=OFF -DSDL_EXAMPLES=OFF -DSDL_INSTALL_TESTS=OFF
        cmake --build "{sdl}/build-switch" -j{args.jobs}
        cd "{sdl}"
        {install_cmd}
    """), devkitpro=dkp)

    # verify the SDL install: sdl3.pc must exist and export the mesa EGL deps,
    # or Taisei's final link fails with undefined eglGetDisplay/etc.
    stage("verifying installed SDL3 (sdl3.pc + EGL export)")
    pc = bash_capture('echo "$PORTLIBS_PREFIX/lib/pkgconfig/sdl3.pc"', dkp)
    if not Path(pc).exists():
        die(f"SDL install did not produce {pc} (build/install step failed?)")
    pc_txt = Path(pc).read_text()
    ver = bash_capture(f'grep -i "^Version:" "{pc}" || true', dkp)
    info(f"installed {ver or 'sdl3 (version unknown)'}")
    if "EGL" in pc_txt:
        info("sdl3.pc exports EGL -> downstream link resolves mesa EGL")
    else:
        info("WARNING: sdl3.pc has no EGL in its link libs; if Taisei fails to "
             "link with undefined eglGetDisplay, the crossfile EGL flags below "
             "are the fallback for exactly that case.")

    # -- 3  Taisei -------------------------------------------------------------
    taisei = work / "taisei"
    if not taisei.exists():
        stage(f"cloning Taisei {TAISEI_TAG} (recursive)")
        run(["git", "clone", "--recurse-submodules", "--depth", "1",
             "-b", TAISEI_TAG, TAISEI_REPO, str(taisei)])

    patch_taisei_switch_source(taisei)
    write_clean_version(taisei)
    fix_nacp_version(taisei)
    add_switch_wallpaper(taisei)
    fix_wallpaper_loader(taisei)
    fix_wallpaper_v2(taisei)
    fix_wallpaper_draw(taisei)
    fix_wallpaper_no_postprocess(taisei)
    fix_wallpaper_pixel_format(taisei)
    fix_wallpaper_blend(taisei)
    fix_wallpaper_frame_start(taisei)

    ensure_pack_deps()

    stage("configuring + building Taisei for Switch")
    dist = work / "dist"
    bash(textwrap.dedent(f"""
        cd "{taisei}"
        # Configure only if not already configured; otherwise resume (ninja is
        # incremental, so a re-run picks up where it stopped).
        # meson only reads cross files on a *fresh* setup (not --reconfigure), so if
        # the existing build wasn't configured with EGL in its crossfile, redo setup.
        need_setup=0
        [ -f build/nx/build.ninja ] || need_setup=1
        if [ -f build/nx/crossfile.txt ]; then
          grep -q -- '-lEGL' build/nx/crossfile.txt || need_setup=1
        else
          need_setup=1
        fi
        if [ "$need_setup" = "1" ]; then
          echo ">> (re)configuring Taisei from scratch (crossfile changed)"
          rm -rf build/nx && mkdir -p build/nx
          ./switch/crossfile.sh > build/nx/crossfile.txt
          # Taisei's own Switch CI option files (still shipped in v1.4.5). These set
          # the correct project options AND the per-subproject werror=false needed to
          # build the vendored shader tools (SPIRV-Cross/glslang/shaderc).
          meson setup build/nx \\
            --cross-file build/nx/crossfile.txt \\
            --cross-file misc/ci/common-options.ini \\
            --cross-file misc/ci/switch-options.ini \\
            -Dwerror=false -Ddocs=disabled \\
            --prefix="{dist}" || (tail -60 build/nx/meson-logs/meson-log.txt; exit 1)
        else
          # Already configured: reconfigure so meson re-runs meson.build. This
          # recomputes meson.project_version() from the now-present .VERSION, which
          # is what feeds the NACP (the version hbmenu shows) -- otherwise it stays
          # stuck at the "1.4.5-dirty-HEAD" value baked at the first configure.
          # (--reconfigure keeps cached machine-file options, so the EGL link args
          #  from the fresh setup are preserved.)
          meson setup build/nx --reconfigure || (tail -60 build/nx/meson-logs/meson-log.txt; exit 1)
        fi
        ninja -C build/nx
        ninja -C build/nx install
    """), devkitpro=dkp)

    # -- 4  collect ------------------------------------------------------------
    nro = taisei / "build/nx/src/taisei.nro"
    if not nro.exists():
        die("build finished but taisei.nro was not produced (check the log above)")
    sd = work / "dist_sdcard/switch/taisei"
    sd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(nro, sd / "taisei.nro")
    data = dist / "data"
    if data.exists():
        shutil.copytree(data, sd / "data", dirs_exist_ok=True)

    print("\n" + c("BUILD COMPLETE", "1;32"))
    print(f"  nro:        {nro}")
    print(f"  sd-card:    {sd}  (copy the 'switch' folder to your SD card root)")
    print("  Launch from hbmenu over a full-memory title (hold R on a game), not applet mode.")

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"a build command failed (exit {e.returncode}). See the output above.")
