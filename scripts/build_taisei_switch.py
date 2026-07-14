#!/usr/bin/env python3
"""
build_taisei_switch.py  —  one-shot Taisei (latest, v1.4.5) -> Nintendo Switch .nro
                           with the SDL3 Switch backend + GLES3 + audio fix.

Run this in an empty folder on a Mac/Linux box that already has devkitPro at
/opt/devkitpro with the Switch portlibs installed (mesa, freetype, libpng, webp,
zstd, opus/ogg/opusfile, cglm, libzip, zlib) plus meson + ninja + cmake.

It will:
  1. clone taisei-project/SDL @ taisei-3.4.10 (the SDL3 fork Taisei v1.4.5 uses)
  2. apply neomody77/sdl3-switch's backend  (video/audio/joystick for libnx)
  3. adapt the video backend for OpenGL ES 3.0 (Taisei needs ES3, the backend
     shipped ES2-only) and fix the audio backend's BGM glitch: 2->4 buffers
     plus a non-blocking drain so WaitDevice() doesn't re-synchronize with
     the hardware after every single buffer
  4. apply three more Switch fixes:
       * clean version label   -- .VERSION override so it reads "1.4.5" and not
         "1.4.5-dirty-<branch>" (we patch the tree, which makes git dirty)
       * Nintendo A/B buttons  -- remap the joystick backend so menus use
         A = accept, B = cancel (the stock backend maps them Xbox-style)
       * clean exit            -- make the audio thread's wait finite and
         shutdown-aware so it doesn't deadlock the shutdown join (which caused
         "The software was closed because an error occurred")
  5. cmake-build that SDL3 static and install it into your Switch portlibs
  6. clone taisei @ v1.4.5 (recursive) and meson/ninja-build the .nro
  7. drop the finished taisei.nro (+ SD-card layout) in ./dist/

Usage:
    python3 build_taisei_switch.py [--workdir DIR] [--jobs N] [--clean]
    DEVKITPRO=/opt/devkitpro is assumed; override with --devkitpro or $DEVKITPRO.
"""

import argparse, os, re, shutil, subprocess, sys, textwrap
from pathlib import Path

TAISEI_TAG = "v1.4.5"
SDL_REPO   = "https://github.com/taisei-project/SDL"
SDL_BRANCH = "taisei-3.4.10"
SDL3SWITCH = "https://github.com/neomody77/sdl3-switch"
TAISEI_REPO = "https://github.com/taisei-project/taisei"

def c(s, code):  # tiny color helper
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
def stage(msg): print("\n" + c(">> " + msg, "1;36"), flush=True)
def info(msg):  print(c("   " + msg, "0;37"), flush=True)
def die(msg):   print(c("!! " + msg, "1;31"), file=sys.stderr); sys.exit(1)

def run(cmd, cwd=None, env=None):
    info("$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str), check=True)

def bash(script, cwd=None, devkitpro=None):
    """Run a bash snippet with devkitPro's Switch environment sourced."""
    env = dict(os.environ)
    if devkitpro:
        env["DEVKITPRO"] = devkitpro
    full = 'set -euo pipefail\nsource "$DEVKITPRO/switchvars.sh"\n' + script
    info("$ (switchvars) " + script.strip().splitlines()[0][:100])
    subprocess.run(["/bin/bash", "-c", full], cwd=cwd, env=env, check=True)

def bash_capture(script, devkitpro):
    env = dict(os.environ); env["DEVKITPRO"] = devkitpro
    full = 'source "$DEVKITPRO/switchvars.sh"\n' + script
    return subprocess.run(["/bin/bash", "-c", full], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()

# ---------------------------------------------------------------------------
# The GLES3 video adaptation (identical logic to adapt_video_gles3.py)
# ---------------------------------------------------------------------------
def adapt_video_gles3(path: Path):
    src = path.read_text()
    if "EGL_OPENGL_ES3_BIT_KHR" in src:
        info("video backend already ES3-adapted"); return

    anchor = "#include <switch.h>\n"
    if anchor not in src:
        die("anchor '#include <switch.h>' not found in SDL_switchvideo.c")
    src = src.replace(anchor, anchor + textwrap.dedent("""
        #ifndef EGL_OPENGL_ES3_BIT_KHR
        #define EGL_OPENGL_ES3_BIT_KHR 0x00000040
        #endif
        #ifndef EGL_CONTEXT_MAJOR_VERSION_KHR
        #define EGL_CONTEXT_MAJOR_VERSION_KHR 0x3098
        #endif
        #ifndef EGL_CONTEXT_MINOR_VERSION_KHR
        #define EGL_CONTEXT_MINOR_VERSION_KHR 0x30FB
        #endif
    """), 1)

    old_attrs = (
        "    static const EGLint config_attrs[] = {\n"
        "        EGL_RED_SIZE, 8,\n"
        "        EGL_GREEN_SIZE, 8,\n"
        "        EGL_BLUE_SIZE, 8,\n"
        "        EGL_ALPHA_SIZE, 8,\n"
        "        EGL_DEPTH_SIZE, 24,\n"
        "        EGL_STENCIL_SIZE, 8,\n"
        "        EGL_SURFACE_TYPE, EGL_WINDOW_BIT,\n"
        "        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,\n"
        "        EGL_NONE\n"
        "    };\n"
        "    static const EGLint context_attrs[] = {\n"
        "        EGL_CONTEXT_CLIENT_VERSION, 2,\n"
        "        EGL_NONE\n"
        "    };"
    )
    new_attrs = textwrap.dedent("""\
        // Honor the GL version/attributes the application requested via
        // SDL_GL_SetAttribute(). Taisei asks for an OpenGL ES 3.0 context and
        // refuses anything below ES 3.0.
            const int req_major = (_this->gl_config.major_version > 0) ? _this->gl_config.major_version : 2;
            const int req_minor = _this->gl_config.minor_version;
            const EGLint renderable = (req_major >= 3) ? EGL_OPENGL_ES3_BIT_KHR : EGL_OPENGL_ES2_BIT;
            const int c_red   = (_this->gl_config.red_size   > 0) ? _this->gl_config.red_size   : 8;
            const int c_green = (_this->gl_config.green_size > 0) ? _this->gl_config.green_size : 8;
            const int c_blue  = (_this->gl_config.blue_size  > 0) ? _this->gl_config.blue_size  : 8;
            const int c_alpha = (_this->gl_config.alpha_size > 0) ? _this->gl_config.alpha_size : 8;
            const int c_depth = (_this->gl_config.depth_size > 0) ? _this->gl_config.depth_size : 24;
            const int c_stencil = _this->gl_config.stencil_size;

            const EGLint config_attrs[] = {
                EGL_RED_SIZE, c_red,
                EGL_GREEN_SIZE, c_green,
                EGL_BLUE_SIZE, c_blue,
                EGL_ALPHA_SIZE, c_alpha,
                EGL_DEPTH_SIZE, c_depth,
                EGL_STENCIL_SIZE, c_stencil,
                EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
                EGL_RENDERABLE_TYPE, renderable,
                EGL_NONE
            };
            EGLint context_attrs[] = {
                EGL_CONTEXT_MAJOR_VERSION_KHR, req_major,
                EGL_CONTEXT_MINOR_VERSION_KHR, req_minor,
                EGL_NONE
            };""")
    if old_attrs not in src:
        die("could not find the ES2 attribute block to replace (backend changed upstream?)")
    src = src.replace(old_attrs, new_attrs)

    old_ctx = (
        "    v->context = eglCreateContext(v->display, v->config, EGL_NO_CONTEXT, context_attrs);\n"
        "    if (v->context == EGL_NO_CONTEXT) {\n"
        '        SDL_SetError("eglCreateContext() failed: %d", eglGetError());\n'
        "        goto error;\n"
        "    }"
    )
    new_ctx = (
        "    v->context = eglCreateContext(v->display, v->config, EGL_NO_CONTEXT, context_attrs);\n"
        "    if (v->context == EGL_NO_CONTEXT) {\n"
        "        // Fallback for EGL without EGL_KHR_create_context.\n"
        "        EGLint fallback_attrs[] = { EGL_CONTEXT_CLIENT_VERSION, req_major, EGL_NONE };\n"
        "        v->context = eglCreateContext(v->display, v->config, EGL_NO_CONTEXT, fallback_attrs);\n"
        "    }\n"
        "    if (v->context == EGL_NO_CONTEXT) {\n"
        '        SDL_SetError("eglCreateContext() failed: %d", eglGetError());\n'
        "        goto error;\n"
        "    }"
    )
    if old_ctx not in src:
        die("could not find the eglCreateContext block to add the fallback")
    src = src.replace(old_ctx, new_ctx)

    path.write_text(src)
    info("video backend adapted for OpenGL ES 3.x")

def fix_audio_backend(header_path: Path, source_path: Path):
    """neomody77/sdl3-switch's stock AUDOUT driver is lockstep: PlayDevice()
    submits one buffer, and WaitDevice() unconditionally blocks on
    audoutWaitPlayFinish() every call. Merely bumping NUM_AUDIO_BUFFERS 2->4
    (the old fix) doesn't change that -- the ring is still only ever one
    buffer deep in practice, so any frame-time hitch on the audio thread
    still drains straight through to an audible BGM glitch, same as v1.4.2's
    stock 2-buffer SDL2 driver had.

    This applies the real fix (mirrors the v1.4.2 SDL2/audren fix, adapted to
    libnx's plain AUDOUT API):
      1. Raise the ring from 2 to 4 buffers.
      2. Track buffers submitted-but-not-yet-released in `queued`.
      3. WaitDevice() first does a non-blocking drain via
         audoutGetReleasedAudioOutBuffer() (a plain IPC call, not a wait) to
         reclaim anything already finished, then returns immediately if the
         ring still has room -- letting the mixer thread get ahead and build
         a real cushion. It only blocks on audoutWaitPlayFinish() once the
         ring is actually full.
    """
    h = header_path.read_text()
    if "int queued;" not in h:
        h2 = h.replace("#define NUM_AUDIO_BUFFERS 2", "#define NUM_AUDIO_BUFFERS 4")
        if "#define NUM_AUDIO_BUFFERS 4" not in h2:
            die("could not find 'NUM_AUDIO_BUFFERS 2' to bump in SDL_switchaudio.h")
        anchor = "    int next;                                  // next pool slot to submit\n"
        if anchor not in h2:
            die("SDL_switchaudio.h: 'next' field line not found in expected form")
        h2 = h2.replace(
            anchor,
            anchor + "    int queued;                                // buffers submitted to AUDOUT, not yet released\n",
            1,
        )
        header_path.write_text(h2)
        info("SDL_switchaudio.h: 2->4 buffers + added in-flight queue counter")
    else:
        info("SDL_switchaudio.h: audio fix already applied")

    c_src = source_path.read_text()
    changed = False

    if "device->hidden->queued = 0;" not in c_src:
        anchor = "    device->hidden->next = 0;\n"
        if anchor not in c_src:
            die("SDL_switchaudio.c: 'next = 0' init line not found")
        c_src = c_src.replace(anchor, anchor + "    device->hidden->queued = 0;\n", 1)
        changed = True

    if "device->hidden->queued++;" not in c_src:
        old_play = (
            '    if (R_FAILED(audoutAppendAudioOutBuffer(b))) {\n'
            '        return SDL_SetError("audoutAppendAudioOutBuffer() failed");\n'
            '    }\n'
            '    return true;\n'
            '}\n'
        )
        new_play = (
            '    if (R_FAILED(audoutAppendAudioOutBuffer(b))) {\n'
            '        return SDL_SetError("audoutAppendAudioOutBuffer() failed");\n'
            '    }\n'
            '    device->hidden->queued++;\n'
            '    return true;\n'
            '}\n'
        )
        if old_play not in c_src:
            die("SDL_switchaudio.c: PlayDevice body not in expected form")
        c_src = c_src.replace(old_play, new_play, 1)
        changed = True

    if "SWITCHAUDIO_DrainReleased" not in c_src:
        old_wait = (
            'static bool SWITCHAUDIO_WaitDevice(SDL_AudioDevice *device)\n'
            '{\n'
            '    AudioOutBuffer *released = NULL;\n'
            '    u32 released_count = 0;\n'
            '    (void)device;\n'
            '    if (R_FAILED(audoutWaitPlayFinish(&released, &released_count, UINT64_MAX))) {\n'
            '        return SDL_SetError("audoutWaitPlayFinish() failed");\n'
            '    }\n'
            '    return true;\n'
            '}\n'
        )
        new_wait = textwrap.dedent('''\
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

                // Ring is full: block until the hardware frees a slot.
                if (R_FAILED(audoutWaitPlayFinish(&released, &released_count, UINT64_MAX))) {
                    return SDL_SetError("audoutWaitPlayFinish() failed");
                }
                if (released_count > (u32)device->hidden->queued) {
                    device->hidden->queued = 0;
                } else {
                    device->hidden->queued -= (int)released_count;
                }
                return true;
            }
        ''')
        if old_wait not in c_src:
            die("SDL_switchaudio.c: WaitDevice body not in expected form")
        c_src = c_src.replace(old_wait, new_wait, 1)
        changed = True

    if changed:
        source_path.write_text(c_src)
        info("SDL_switchaudio.c: non-blocking drain + cushioned WaitDevice applied")
    else:
        info("SDL_switchaudio.c: audio fix already applied")

# ---------------------------------------------------------------------------
def patch_sdl_cmake_egl_link(sdl_root):
    """SDL's Switch CMake block adds EGL/GLES *headers* but never links libEGL,
    so sdl3.pc doesn't tell downstream apps (Taisei) to link it -> undefined
    eglGetDisplay/etc at final link. Add the mesa EGL libs as a link dependency,
    which sdl_link_dependency also writes into sdl3.pc's Libs.private."""
    cml = sdl_root / "CMakeLists.txt"
    if not cml.exists():
        info("SDL CMakeLists.txt not found; skipping EGL link fix"); return
    t = cml.read_text()
    if "sdl_link_dependency(switch_egl" in t:
        info("SDL CMake: EGL link dependency already present"); return
    anchor = ('    sdl_include_directories(PRIVATE SYSTEM "$ENV{DEVKITPRO}/portlibs/switch/include")\n'
              '    set(HAVE_SDL_VIDEO TRUE)')
    if anchor not in t:
        info("SDL CMake: switch video block not in expected form; skipping"); return
    repl = ('    sdl_include_directories(PRIVATE SYSTEM "$ENV{DEVKITPRO}/portlibs/switch/include")\n'
            '    # switch-mesa EGL + its static deps must be on the link line and exported\n'
            '    # via sdl3.pc so downstream apps (Taisei) resolve eglGetDisplay/etc.\n'
            '    sdl_link_dependency(switch_egl LIBS EGL glapi drm_nouveau)\n'
            '    set(HAVE_SDL_VIDEO TRUE)')
    cml.write_text(t.replace(anchor, repl, 1))
    info("SDL CMake: added EGL link dependency (EGL glapi drm_nouveau)")


def fix_wallpaper_draw(taisei_root):
    """The wallpaper quad was culled: draw_framebuffer_attachment() (which works
    in the same spot) explicitly sets r_cull(CULL_BACK), but our draw ran before
    that with whatever cull state the 3D/postprocess left (often CULL_FRONT), so
    the quad got culled away. Set CULL_BACK for our draw (and restore whatever
    cull mode was active before, rather than clobbering it for later draws)."""
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
    The debug log confirms this: the one-time "draw:" line never got written.

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
    """nx_wallpaper_draw() saves/restores shader, color, cull mode, framebuffer and
    scissor via r_state_push()/r_state_pop() -- but per Taisei's own
    renderer/common/state.c, that stack only restores whatever was actually
    *touched* between push and pop (a dirty-bit system, not a full snapshot).
    We never call r_blend(), so the draw runs with whatever blend mode was left
    over from the previous draw call that frame (almost certainly BLEND_ALPHA
    somewhere in Taisei's own rendering). If wallpaper.png has any real alpha in
    it -- transparent padding if the art doesn't fill the full canvas, a
    dither/pattern with partial alpha, anti-aliased edges, etc -- those pixels
    blend toward the black clear color instead of showing their real RGB. That
    reads exactly as scattered missing pixels and a black region at the bottom.
    Force BLEND_NONE so the wallpaper always draws fully opaque."""
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
    """Architectural fix for the settings-menu text going missing whenever the
    wallpaper is on. nx_wallpaper_draw() was called at the very END of the
    frame -- inside video_swap_buffers(), right before the buffer flip -- which
    means it painted an *opaque* quad over the bars after everything else that
    frame (game, UI, menus) had already been drawn to the screen framebuffer.
    That's backwards for something meant to be a background: if any menu/UI
    element extends into the pillarbox bars, our end-of-frame overpaint just
    silently erases it. That's almost certainly the missing-text bug -- and
    it's a real risk for any other UI screen too, not just settings.

    Taisei's actual per-frame entry point is run_render_frame() in
    src/eventloop/eventloop.c: it clears the screen to black, then calls
    frame->render() to draw that frame's content. Moving our draw to right
    after that clear (before frame->render()) makes the wallpaper a genuine
    background -- everything drawn afterwards layers on top of it normally,
    so it can never occlude anything, regardless of what draws where. This
    also removes the need for two separate, awkward call sites inside
    video_swap_buffers() (one per postprocess / no-postprocess branch): one
    unconditional call, once a frame, before any content exists to hide."""
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
    """Robust wallpaper load. Reads wallpaper.png with plain fopen/fread (the
    same newlib path the VFS already uses for data/) instead of SDL_IOFromFile,
    which proved unreliable for this file."""
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
    info("video.c: wallpaper loader -> fopen/fread (no more debug log)")


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
            new = 'ADDITIONAL_LINK_FLAGS="-specs=$DEVKITPRO/libnx/switch.specs -lEGL -lglapi -ldrm_nouveau"'
            if old not in t:
                die("switch/crossfile.sh: ADDITIONAL_LINK_FLAGS line not found")
            cf.write_text(t.replace(old, new, 1))
            info("switch/crossfile.sh: added mesa EGL to link flags")
        else:
            info("switch/crossfile.sh: EGL link flags already present")


def patch_sdl_egl_switch(sdl_root):
    """SDL's bundled SDL_egl.h has no case for the Switch, so it #errors on the
    EGL native types when Taisei's gles.c includes it. Add a __SWITCH__ branch."""
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
    info("SDL_egl.h: added Switch EGL native types")


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


def fix_video_clean_exit(video_path):
    """On Switch, mesa/nouveau EGL teardown (eglDestroyContext/eglDestroySurface/
    eglTerminate) can deadlock the GPU channel during shutdown. Taisei runs this
    only at process exit and never recreates the context (fullscreen-only), so the
    graceful teardown buys nothing but a hang -- the app never reaches
    appletUnlockExit() and the system reports 'software closed because an error
    occurred'. Drop our EGL handles and let the kernel reclaim the GPU on exit."""
    t = video_path.read_text()
    if "let the kernel reclaim the GPU" in t:
        info("SDL_switchvideo.c: clean-exit teardown already applied"); return

    old_unload = (
        "static void SWITCH_GL_UnloadLibrary(SDL_VideoDevice *_this)\n"
        "{\n"
        "    SDL_VideoData *v = _this->internal;\n"
        "    if (v && v->display != EGL_NO_DISPLAY) {\n"
        "        eglTerminate(v->display);\n"
        "        v->display = EGL_NO_DISPLAY;\n"
        "    }\n"
        "}"
    )
    new_unload = (
        "static void SWITCH_GL_UnloadLibrary(SDL_VideoDevice *_this)\n"
        "{\n"
        "    SDL_VideoData *v = _this->internal;\n"
        "    // Skip mesa/nouveau eglTerminate(): it can hang the GPU channel on\n"
        "    // exit. Just forget the handle and let the kernel reclaim the GPU.\n"
        "    if (v) {\n"
        "        v->display = EGL_NO_DISPLAY;\n"
        "    }\n"
        "}"
    )
    if old_unload not in t:
        die("SDL_switchvideo.c: SWITCH_GL_UnloadLibrary not in expected form")
    t = t.replace(old_unload, new_unload, 1)

    old_destroy = (
        "static bool SWITCH_GL_DestroyContext(SDL_VideoDevice *_this, SDL_GLContext context)\n"
        "{\n"
        "    SDL_VideoData *v = _this->internal;\n"
        "    (void)context;\n"
        "    if (v->display != EGL_NO_DISPLAY) {\n"
        "        eglMakeCurrent(v->display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);\n"
        "        if (v->context != EGL_NO_CONTEXT) {\n"
        "            eglDestroyContext(v->display, v->context);\n"
        "            v->context = EGL_NO_CONTEXT;\n"
        "        }\n"
        "        if (v->surface != EGL_NO_SURFACE) {\n"
        "            eglDestroySurface(v->display, v->surface);\n"
        "            v->surface = EGL_NO_SURFACE;\n"
        "        }\n"
        "    }\n"
        "    v->egl_initialized = false;\n"
        "    return true;\n"
        "}"
    )
    new_destroy = (
        "static bool SWITCH_GL_DestroyContext(SDL_VideoDevice *_this, SDL_GLContext context)\n"
        "{\n"
        "    SDL_VideoData *v = _this->internal;\n"
        "    (void)context;\n"
        "    // Skip the mesa/nouveau EGL teardown (see SWITCH_GL_UnloadLibrary): it\n"
        "    // can deadlock the GPU channel and hang the process on exit. Just drop\n"
        "    // our handles; the kernel reclaims the GPU when the process terminates.\n"
        "    v->context = EGL_NO_CONTEXT;\n"
        "    v->surface = EGL_NO_SURFACE;\n"
        "    v->egl_initialized = false;\n"
        "    return true;\n"
        "}"
    )
    if old_destroy not in t:
        die("SDL_switchvideo.c: SWITCH_GL_DestroyContext not in expected form")
    t = t.replace(old_destroy, new_destroy, 1)

    video_path.write_text(t)
    info("SDL_switchvideo.c: EGL teardown neutralized for clean Switch exit")


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
    """version.py runs `git describe --dirty`; because we patch the tree it
    yields e.g. 'v1.4.5-dirty-<branch>'. A .VERSION override file (checked
    before git) pins a clean label."""
    vf = taisei_root / ".VERSION"
    want = TAISEI_TAG  # e.g. "v1.4.5"
    if vf.exists() and vf.read_text().strip() == want:
        info(".VERSION already clean"); return
    vf.write_text(want + "\n")
    info(f".VERSION set to {want} (clean version label, no -dirty suffix)")


def fix_gamepad_ab(sdl_root):
    """The switch joystick backend maps face buttons positionally (Xbox style:
    SDL a<-physical A). Nintendo's SDL convention is SDL SOUTH(a)=physical B,
    EAST(b)=physical A, and Taisei's __SWITCH__ code assumes that. The mismatch
    makes menus treat A as cancel / B as accept. Map like a real Nintendo pad."""
    js = sdl_root / "src/joystick/switch/SDL_sysjoystick.c"
    if not js.exists():
        info("SDL_sysjoystick.c not found; skipping A/B fix"); return
    t = js.read_text()
    old = ("    out->a = (SDL_InputMapping){ EMappingKind_Button, 0 };\n"
           "    out->b = (SDL_InputMapping){ EMappingKind_Button, 1 };\n"
           "    out->x = (SDL_InputMapping){ EMappingKind_Button, 2 };\n"
           "    out->y = (SDL_InputMapping){ EMappingKind_Button, 3 };\n")
    new = ("    out->a = (SDL_InputMapping){ EMappingKind_Button, 1 };  // SDL SOUTH <- physical B\n"
           "    out->b = (SDL_InputMapping){ EMappingKind_Button, 0 };  // SDL EAST  <- physical A\n"
           "    out->x = (SDL_InputMapping){ EMappingKind_Button, 3 };  // SDL WEST  <- physical Y\n"
           "    out->y = (SDL_InputMapping){ EMappingKind_Button, 2 };  // SDL NORTH <- physical X\n")
    if new in t:
        info("SDL_sysjoystick.c: A/B mapping already Nintendo-correct"); return
    if old not in t:
        info("SDL_sysjoystick.c: face-button mapping not in expected form; skipping"); return
    js.write_text(t.replace(old, new, 1))
    info("SDL_sysjoystick.c: face buttons remapped to Nintendo layout (A=accept, B=cancel)")


def fix_audio_clean_exit(source_path):
    """The audio thread blocks in audoutWaitPlayFinish(UINT64_MAX). At shutdown
    ClosePhysicalAudioDevice() sets device->shutdown then SDL_WaitThread()s the
    audio thread -- but a queued buffer may never be released once playback is
    stopping, so the unbounded wait never returns, the join hangs, and the
    system reports 'software closed because an error occurred'. Make the wait
    finite + shutdown-aware so the thread can exit cleanly."""
    t = source_path.read_text()
    if "SDL_GetAtomicInt(&device->shutdown)" in t:
        info("SDL_switchaudio.c: clean-exit wait already applied"); return
    old = (
        "    // Ring is full: block until the hardware frees a slot.\n"
        "    if (R_FAILED(audoutWaitPlayFinish(&released, &released_count, UINT64_MAX))) {\n"
        '        return SDL_SetError("audoutWaitPlayFinish() failed");\n'
        "    }\n"
        "    if (released_count > (u32)device->hidden->queued) {\n"
        "        device->hidden->queued = 0;\n"
        "    } else {\n"
        "        device->hidden->queued -= (int)released_count;\n"
        "    }\n"
        "    return true;\n"
        "}\n"
    )
    new = (
        "    // Ring is full: block until the hardware frees a slot, but wake up\n"
        "    // periodically so we can observe a shutdown request. A queued buffer\n"
        "    // may never be released once playback is stopping at teardown, so an\n"
        "    // unbounded wait here deadlocks the SDL_WaitThread() join in\n"
        "    // ClosePhysicalAudioDevice() -- the app then hangs on exit and the\n"
        '    // system reports "software closed because an error occurred".\n'
        "    while (!SDL_GetAtomicInt(&device->shutdown)) {\n"
        "        released = NULL;\n"
        "        released_count = 0;\n"
        "        if (R_SUCCEEDED(audoutWaitPlayFinish(&released, &released_count, 100000000ULL))) {\n"
        "            if (released_count > (u32)device->hidden->queued) {\n"
        "                device->hidden->queued = 0;\n"
        "            } else {\n"
        "                device->hidden->queued -= (int)released_count;\n"
        "            }\n"
        "            break;\n"
        "        }\n"
        "        // timed out with nothing released: loop and re-check shutdown\n"
        "    }\n"
        "    return true;\n"
        "}\n"
    )
    if old not in t:
        die("SDL_switchaudio.c: cushioned WaitDevice blocking tail not found (audio fix changed?)")
    source_path.write_text(t.replace(old, new, 1))
    info("SDL_switchaudio.c: finite, shutdown-aware audio wait (clean exit) applied")


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

    # -- 1..3  patched SDL3 ---------------------------------------------------
    sdl = work / "SDL"
    if not sdl.exists():
        stage("cloning taisei SDL3 fork (taisei-3.4.10)")
        run(["git", "clone", "--depth", "1", "-b", SDL_BRANCH, SDL_REPO, str(sdl)])

    video_c = sdl / "src/video/switch/SDL_switchvideo.c"
    if not video_c.exists():
        stage("fetching + applying neomody77/sdl3-switch backend")
        s3 = work / "sdl3-switch"
        if not s3.exists():
            run(["git", "clone", "--depth", "1", SDL3SWITCH, str(s3)])
        # patch paths are a/external/SDL3/... -> strip 3 components
        try:
            run(["git", "apply", "-p3", str(s3 / "sdl3-switch.patch")], cwd=str(sdl))
        except subprocess.CalledProcessError:
            run(["patch", "-p3", "-i", str(s3 / "sdl3-switch.patch")], cwd=str(sdl))
    else:
        info("switch backend already present in SDL tree")

    stage("adapting backend: GLES3 context + non-blocking cushioned audio queue")
    adapt_video_gles3(video_c)
    fix_video_clean_exit(video_c)
    fix_audio_backend(
        sdl / "src/audio/switch/SDL_switchaudio.h",
        sdl / "src/audio/switch/SDL_switchaudio.c",
    )
    fix_audio_clean_exit(sdl / "src/audio/switch/SDL_switchaudio.c")
    fix_gamepad_ab(sdl)
    patch_sdl_egl_switch(sdl)
    patch_sdl_cmake_egl_link(sdl)

    # -- 4  build + install SDL3 into portlibs --------------------------------
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

    # -- 5  Taisei -------------------------------------------------------------
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

    # -- 6  collect ------------------------------------------------------------
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