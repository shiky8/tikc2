#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
from playwright.async_api import async_playwright
import json
import os
import re
import shutil
import subprocess
import tempfile
import random
import time
from typing import Dict, List, Tuple, Optional
from moviepy.editor import TextClip, concatenate_videoclips, AudioFileClip
import random

# ---- Audio (offline first) ----
def synth_speech(text: str, outfile: str = "narration.wav", voice_engine: str = "auto") -> str:
    """
    Generate speech from text.
    - Tries pyttsx3 (offline). If missing/fails and voice_engine != 'pyttsx3', falls back to gTTS (online).
    Returns the audio filename created.
    """
    # prefer offline pyttsx3
    if voice_engine in ("auto", "pyttsx3"):
        try:
            import pyttsx3  # type: ignore
            engine = pyttsx3.init()
            # slightly slower rate for tutorial clarity
            rate = engine.getProperty("rate")
            engine.setProperty("rate", int(rate * 0.85))
            engine.save_to_file(text, outfile)
            engine.runAndWait()
            if os.path.exists(outfile) and os.path.getsize(outfile) > 1000:
                return outfile
        except Exception:
            if voice_engine == "pyttsx3":
                raise

    # fallback to gTTS (needs internet)
    if voice_engine in ("auto", "gtts"):
        try:
            from gtts import gTTS  # type: ignore
            mp3 = os.path.splitext(outfile)[0] + ".mp3"
            gTTS(text=text, lang="en").save(mp3)
            return mp3
        except Exception as e:
            raise RuntimeError(f"Failed to synthesize voice with gTTS: {e}")

    raise RuntimeError("No TTS backend succeeded.")


# ---- Man page parsing ----
def read_man_page(cmd: str, timeout: int = 5) -> str:
    """
    Read the man page raw text for a command. Uses 'man -P cat'.
    Returns empty string if unavailable.
    """
    try:
        # Use 'man -P cat' to dump raw text to stdout
        res = subprocess.run(
            ["man", "-P", "cat", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return res.stdout or ""
    except Exception:
        return ""


def extract_sections(man_text: str) -> Dict[str, str]:
    """
    Roughly split a man page into sections: NAME, SYNOPSIS, DESCRIPTION, OPTIONS
    Works best for typical man page formats but is resilient if formats vary.
    """
    # Normalize line endings
    txt = man_text.replace("\r\n", "\n")

    # Find all all-caps headings
    headings = list(re.finditer(r"^\s*([A-Z][A-Z0-9 _-]{2,})\s*$", txt, flags=re.MULTILINE))
    sections: Dict[str, str] = {}
    if not headings:
        return sections

    for i, m in enumerate(headings):
        title = m.group(1).strip()
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(txt)
        sections[title] = txt[start:end].strip()

    # Common aliases mapping
    normalized = {}
    for k, v in sections.items():
        key = k.strip()
        if "NAME" in key:
            normalized["NAME"] = v
        elif "SYNOPSIS" in key:
            normalized["SYNOPSIS"] = v
        elif "DESCRIPTION" in key:
            normalized["DESCRIPTION"] = v
        elif "OPTIONS" in key or "OPTION" in key:
            normalized["OPTIONS"] = v
    return normalized


def name_one_liner(cmd: str, sections: Dict[str, str]) -> str:
    """
    Extract a friendly one-liner from the NAME section: e.g. "id - print user identity".
    """
    name = sections.get("NAME", "").splitlines()
    for line in name:
        line = line.strip()
        # Typical format: "id - print real and effective user and group IDs"
        if " - " in line:
            return re.sub(r"\s+", " ", line)
        # Some manpages: "id — print ..." (em-dash)
        if " — " in line:
            return re.sub(r"\s+", " ", line)
    # Fallback: use first non-empty description line
    desc = sections.get("DESCRIPTION", "").splitlines()
    for line in desc:
        if line.strip():
            return f"{cmd} - {line.strip()[:120]}"
    return f"{cmd} - Linux command."


def synopsis_summary(sections: Dict[str, str]) -> str:
    """
    Compress the SYNOPSIS to one line (if available).
    """
    syn = sections.get("SYNOPSIS", "").strip()
    if not syn:
        return ""
    # Collapse whitespace, keep to a reasonable length
    s = re.sub(r"\s+", " ", syn).strip()
    return s[:240]


def parse_options_block(options_text: str, max_opts: int = 8) -> List[Tuple[str, str]]:
    """
    Extract option flags and their short descriptions from OPTIONS section.
    Returns list of (flag, summary).
    """
    if not options_text:
        return []

    lines = options_text.splitlines()
    pairs: List[Tuple[str, str]] = []

    # Heuristic: option definitions often start with spaces then -x or --long
    opt_re = re.compile(r"^\s{0,12}(-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*)(?:,\s*(-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*))?\s*(.*)$")

    i = 0
    while i < len(lines) and len(pairs) < max_opts:
        m = opt_re.match(lines[i])
        if m:
            flags = [m.group(1)]
            if m.group(2):
                flags.append(m.group(2))
            # Grab following lines that look like the paragraph for this option
            desc_lines = [m.group(3).strip()] if m.group(3) else []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if opt_re.match(nxt):  # next option starts
                    break
                if nxt.strip() == "":
                    # Keep a single blank as paragraph separator
                    if desc_lines and desc_lines[-1] != "":
                        desc_lines.append("")
                else:
                    desc_lines.append(nxt.strip())
                j += 1
            desc = " ".join([x for x in desc_lines if x != ""]).strip()
            flags_str = ", ".join(flags)
            if desc:
                # Trim long descriptions
                desc = re.sub(r"\s+", " ", desc)
                if len(desc) > 200:
                    desc = desc[:200].rstrip() + "..."
                pairs.append((flags_str, desc))
            else:
                pairs.append((flags_str, ""))

            i = j
        else:
            i += 1

    return pairs


def build_explanation(cmd: str, sections: Dict[str, str], options: List[Tuple[str, str]]) -> str:
    """
    Create a friendly explanation paragraph from man page sections.
    """
    one_liner = name_one_liner(cmd, sections)
    syn = synopsis_summary(sections)
    desc = sections.get("DESCRIPTION", "").splitlines()
    # Pick first meaningful description line
    first_para = ""
    for line in desc:
        if line.strip() and not line.strip().startswith("."):
            first_para = line.strip()
            break
    first_para = re.sub(r"\s+", " ", first_para)[:350] if first_para else ""

    bits = []
    bits.append(f"{one_liner}.")
    if syn:
        bits.append(f"The basic syntax is: {syn}.")
    if first_para:
        bits.append(first_para)

    if options:
        bits.append("Key options include: " + "; ".join([f"{f} which {d}" if d else f"{f}" for f, d in options[:4]]) + ".")

    return " ".join(bits)


def analyze_command_output(cmd: str, output: str) -> str:
    """
    Analyze the command output and provide detailed explanation.
    """
    if not output or output == "[no output provided]":
        return f"The {cmd} command completed successfully with no output to display."
    
    lines = output.strip().split('\n')
    analysis_parts = []
    
    # Analyze based on command type
    if cmd == 'id':
        analysis_parts.append("The output shows your user and group information.")
        if 'uid=' in output:
            uid_match = re.search(r'uid=(\d+)\(([^)]+)\)', output)
            if uid_match:
                analysis_parts.append(f"Your user ID is {uid_match.group(1)} with username {uid_match.group(2)}.")
        if 'gid=' in output:
            gid_match = re.search(r'gid=(\d+)\(([^)]+)\)', output)
            if gid_match:
                analysis_parts.append(f"Your primary group ID is {gid_match.group(1)} named {gid_match.group(2)}.")
    
    elif cmd in ['ls', 'ls -l', 'ls -la']:
        analysis_parts.append(f"The output shows {len(lines)} items in the current directory.")
        if any(line.startswith('d') for line in lines):
            dirs = sum(1 for line in lines if line.startswith('d'))
            analysis_parts.append(f"There are {dirs} directories shown.")
        if any(line.startswith('-') for line in lines):
            files = sum(1 for line in lines if line.startswith('-'))
            analysis_parts.append(f"There are {files} regular files listed.")
    
    elif cmd == 'pwd':
        analysis_parts.append(f"You are currently in the directory: {output.strip()}.")
        if output.strip() == '/':
            analysis_parts.append("This is the root directory of the filesystem.")
        elif output.strip().startswith('/home/'):
            analysis_parts.append("This appears to be in a user's home directory area.")
    
    elif cmd in ['uname', 'uname -a']:
        analysis_parts.append("The output shows system information.")
        if len(lines) == 1 and ' ' in output:
            parts = output.split()
            analysis_parts.append(f"The system is running {parts[0]} kernel version {parts[2] if len(parts) > 2 else 'unknown'}.")
    
    elif cmd == 'whoami':
        analysis_parts.append(f"You are currently logged in as user: {output.strip()}.")
    
    elif cmd == 'date':
        analysis_parts.append(f"The current system date and time is: {output.strip()}.")
    
    else:
        # Generic analysis
        analysis_parts.append(f"The {cmd} command produced {len(lines)} line{'s' if len(lines) != 1 else ''} of output.")
        if len(output) > 200:
            analysis_parts.append("The output contains detailed information about the system or files.")
        
    # Add first line explanation for all commands
    if lines and lines[0].strip():
        first_line = lines[0].strip()
        if len(first_line) < 100:
            analysis_parts.append(f"The first line shows: {first_line}")
    
    return " ".join(analysis_parts)


# ---- Command runner (optional) ----
def run_command_capture_output(cmd: str, timeout: int = 4) -> str:
    """
    Safely run a command (no shell), capture stdout/stderr.
    Splits by spaces; does not support complex shell syntax for safety.
    """
    parts = [p for p in cmd.strip().split() if p]
    if not parts:
        return ""
    try:
        res = subprocess.run(
            parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return (res.stdout or "").strip()
    except Exception as e:
        return f"[error running command: {e}]"


def create_terminal_header() -> str:
    """Create a realistic terminal header"""
    return "┌─ Terminal ─────────────────────────────────────────────┐\n│  shiky8@linux:~$ \n"


def create_terminal_footer() -> str:
    """Create terminal footer"""
    return "└────────────────────────────────────────────────────────┘"


# ---- Enhanced Video assembly ----
def make_video(cmd: str, output_text: str, narration_text: str, outfile: str = "linux_tutorial.mp4", fps: int = 24):
    """
    Build an enhanced video with realistic terminal appearance and detailed explanations.
    Fixed to avoid MoviePy compositing and color format issues.
    """
    

    # 1) Generate narration audio
    audio_file = synth_speech(narration_text, "narration.wav", voice_engine="auto")
    audio = AudioFileClip(audio_file)
    audio_duration = audio.duration
    print(f"Audio duration: {audio_duration:.2f} seconds")

    # 2) Video settings - using simpler approach
    size = (1280, 720)
    font = "DejaVu-Sans-Mono"
    
    # 3) Calculate timing
    intro_duration = min(4.0, audio_duration * 0.2)
    explanation_duration = min(10.0, audio_duration * 0.25) 
    typing_duration = min(5.0, audio_duration * 0.2)
    output_duration = max(3.0, audio_duration * 0.25)
    outro_duration = min(2.0, audio_duration * 0.2)
    
    # Adjust if total exceeds audio duration
    total_planned = intro_duration + explanation_duration + typing_duration + output_duration + outro_duration
    if total_planned > audio_duration:
        scale_factor = audio_duration / total_planned
        intro_duration *= scale_factor
        explanation_duration *= scale_factor
        typing_duration *= scale_factor
        output_duration *= scale_factor
        outro_duration *= scale_factor

    clips = []

    # 4) Intro slide - simple approach
    intro_text = f"""
    ╔═══════════════════════════════════════════════════╗
    ║        Linux Command Tutorial                     ║
    ║                                                   ║
    ║              {cmd.upper():^15}                    ║
    ║                                                   ║
    ║        Learn Linux Commands wiht shiky8!          ║
    ╚═══════════════════════════════════════════════════╝
    """
    
    try:
        intro_clip = TextClip(intro_text, fontsize=32, color="white", 
                             size=size, method="caption").set_duration(intro_duration)
        clips.append(intro_clip)
    except Exception as e:
        print(f"Warning: Could not create intro with fancy formatting: {e}")
        intro_clip = TextClip(f"Linux Command Tutorial\n\n{cmd.upper()}\n\nLearn Linux Commands!", 
                             fontsize=36, color="white", font=font,
                             size=size, method="caption").set_duration(intro_duration)
        clips.append(intro_clip)

    # 5) Command explanation slide
    man_txt = read_man_page(cmd)
    sections = extract_sections(man_txt) if man_txt else {}
    one_liner = name_one_liner(cmd, sections) if sections else f"{cmd} - Linux command"
    
    explanation_text = f"Understanding: {cmd}\n\n{one_liner}\n\nLet's see it in action..."
    explanation_clip = TextClip(explanation_text, fontsize=28, color="white", font=font,
                               size=size, method="caption").set_duration(explanation_duration)
    clips.append(explanation_clip)

    # 6) Terminal simulation - step by step typing
    terminal_prompt = "shiky8@linux:~$ "
    full_command = f"{terminal_prompt}{cmd}"
    
    # Create typing animation frames
    typing_clips = []
    chars_typed = ""
    
    # Calculate timing for typing
    chars_per_second = 8  # Reasonable typing speed
    total_chars = len(full_command)
    char_duration = min(1, typing_duration / max(total_chars, 1))
    
    for i, char in enumerate(full_command):
        chars_typed += char
        
        # Create terminal display with current text
        terminal_display = f"""
╔════════════════ Terminal ════════════════╗
║                                          ║
║  {chars_typed:<36}█  ║
║                                          ║
╚══════════════════════════════════════════╝
        """
        
        # Add some variation in typing speed
        frame_duration = char_duration * random.uniform(0.5, 1.5)
        
        try:
            typing_frame = TextClip(terminal_display, fontsize=20, color="#00ff00", 
                                  font=font, size=size, method="caption").set_duration(frame_duration)
            typing_clips.append(typing_frame)
        except Exception:
            # Fallback to simple text if terminal display fails
            simple_text = f"Typing: {chars_typed}█"
            typing_frame = TextClip(simple_text, fontsize=24, color="#00ff00", 
                                  font=font, size=size, method="caption").set_duration(frame_duration)
            typing_clips.append(typing_frame)
    
    # Final command without cursor
    final_terminal = f"""
╔════════════════ Terminal ════════════════╗
║                                          ║
║  {full_command:<38}  ║
║                                          ║
╚══════════════════════════════════════════╝
    """
    
    try:
        final_frame = TextClip(final_terminal, fontsize=20, color="#00ff00", 
                              font=font, size=size, method="caption").set_duration(0.5)
        typing_clips.append(final_frame)
    except Exception:
        final_frame = TextClip(f"Command: {full_command}", fontsize=24, color="#00ff00", 
                              font=font, size=size, method="caption").set_duration(0.5)
        typing_clips.append(final_frame)
    
    # Combine typing sequence
    if typing_clips:
        typing_sequence = concatenate_videoclips(typing_clips)
        if typing_sequence.duration > typing_duration:
            typing_sequence = typing_sequence.subclip(0, typing_duration)
        clips.append(typing_sequence)

    # 7) Output display
    if output_text and output_text != "[no output provided]":
        output_lines = output_text.strip().split('\n')  # Limit lines to avoid overflow
        
        # Create progressive output display
        output_clips = []
        current_output = []
        
        line_duration = min(1.6, output_duration / max(len(output_lines), 2))
        
        for i, line in enumerate(output_lines):
            current_output.append(line)  # Limit line length
            
            # Build terminal display with command and current output
            output_display = f"""
╔════════════════ Terminal ════════════════╗
║                                          ║
║  {full_command}  ║"""
            
            for output_line in current_output:
                output_display += f"\n║  {output_line}  ║"
            
            # Pad to consistent height
            while output_display.count('\n') < 8:
                output_display += "\n║                                          ║"
            
            output_display += "\n╚══════════════════════════════════════════╝"
            
            try:
                output_frame = TextClip(output_display, fontsize=18, color="#00ff00", 
                                      font=font, size=size, method="caption").set_duration(line_duration)
                output_clips.append(output_frame)
            except Exception:
                # Fallback to simple output display
                simple_output = f"Command: {full_command}\n\nOutput:\n" + "\n".join(current_output)
                output_frame = TextClip(simple_output, fontsize=20, color="#00ff00", 
                                      font=font, size=size, method="caption").set_duration(line_duration)
                output_clips.append(output_frame)
        
        # Hold final output
        remaining_time = max(1.0, output_duration - (len(output_clips) * line_duration))
        if output_clips:
            final_output = output_clips[-1].set_duration(remaining_time)
            output_clips[-1] = final_output
            
            output_sequence = concatenate_videoclips(output_clips)
            clips.append(output_sequence)
    else:
        # No output display
        no_output_display = f"""
╔════════════════ Terminal ════════════════╗
║                                          ║
║  {full_command:<38}  ║
║                                          ║
║  [Command completed successfully]        ║
║                                          ║
╚══════════════════════════════════════════╝
        """
        
        try:
            no_output_clip = TextClip(no_output_display, fontsize=20, color="white", 
                                    font=font, size=size, method="caption").set_duration(output_duration)
        except Exception:
            no_output_clip = TextClip(f"Command: {full_command}\n\n[Completed successfully]", 
                                    fontsize=24, color="white", font=font,
                                    size=size, method="caption").set_duration(output_duration)
        clips.append(no_output_clip)

    # 8) Outro with summary
    outro_text = f"""
    Summary
    
    ✓ Command: {cmd}
    ✓ Linux system command
    ✓ Use 'man {cmd}' for details

     ✓ see you in the next video with shiky8
    
    Happy Learning! 🐧
    """
    
    try:
        outro_clip = TextClip(outro_text, fontsize=28, color="white", font=font,
                             size=size, method="caption").set_duration(outro_duration)
    except Exception:
        outro_clip = TextClip(f"Summary\n\nCommand: {cmd}\nUse 'man {cmd}' for more info\n\nHappy Learning!", 
                             fontsize=24, color="white", font=font,
                             size=size, method="caption").set_duration(outro_duration)
    clips.append(outro_clip)

    # 9) Combine all clips with robust error handling
    try:
        if clips:
            video = concatenate_videoclips(clips)
        else:
            raise Exception("No clips created")
            
    except Exception as e:
        print(f"Error creating video: {e}")
        print("Creating simple fallback video...")
        
        # Simple fallback video
        fallback_text = f"""
Linux Command Tutorial: {cmd}

Command: {full_command}

{one_liner if 'one_liner' in locals() else f"{cmd} - Linux command"}

Output:
{output_text[:400] if output_text else "No output"}

Use 'man {cmd}' for more information.
        """
        
        video = TextClip(fallback_text, fontsize=20, color="white", font=font,
                        size=size, method="caption").set_duration(audio_duration)

    # 10) Audio synchronization
    try:
        final_duration = min(video.duration, audio_duration)
        
        if video.duration != final_duration:
            video = video.subclip(0, final_duration)
        if audio.duration != final_duration:
            audio = audio.subclip(0, final_duration)
            
        video = video.set_audio(audio)
        
    except Exception as e:
        print(f"Error with audio sync: {e}")
        video = video.set_duration(audio_duration).set_audio(audio)
    
    print(f"Final video duration: {video.duration:.2f} seconds")

    # 11) Export with conservative settings
    try:
        video.write_videofile(outfile, fps=fps, codec="libx264", audio_codec="aac", 
                             threads=2, preset="fast", temp_audiofile="temp-audio.m4a", 
                             remove_temp=True)
    except Exception as e:
        print(f"Error writing video: {e}")
        # Try with most basic settings
        video.write_videofile(outfile, fps=fps)

    # Cleanup
    try:
        os.remove(audio_file)
    except Exception:
        pass

    print(f"Video created successfully: {outfile}")


def build_enhanced_narration(cmd: str, output_text: str) -> str:
    """
    Compose a comprehensive voiceover with detailed explanations.
    """
    # Get man page info
    man_txt = read_man_page(cmd)
    sections = extract_sections(man_txt) if man_txt else {}
    options = parse_options_block(sections.get("OPTIONS", "")) if sections else []
    command_explanation = build_explanation(cmd, sections, options)
    
    # Analyze the output
    output_analysis = analyze_command_output(cmd, output_text)
    
    # Build comprehensive narration
    narration_parts = [
        f"Welcome to this Linux command tutorial. Today we're exploring the {cmd} command.",
        command_explanation,
        f"Now let's execute {cmd} and examine its output.",
        output_analysis,
        "Understanding command output helps you effectively use Linux tools.",
        f"For more detailed information, you can always use 'man {cmd}' to read the manual page.",
        "This concludes our tutorial. Keep practicing with Linux commands to build your skills."
    ]
    
    return " ".join(narration_parts)






async def upload_video(video_path, description, cookies_path):
    async with async_playwright() as p:
        # Use installed Google Chrome instead of bundled Chromium
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="/tmp/playwright",   # persistent profile storage
            executable_path="/usr/bin/google-chrome-stable",  # system Chrome
            headless=True,
             locale="en-US",
    viewport={"width": 1365, "height": 900},
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--enable-features=VaapiVideoDecoder",  # (optional, for hw accel)
            ]
        )

        page = await browser.new_page()

#         # context = await browser.new_context()
#         context = await browser.new_context(
#     locale="en-US",
#     viewport={"width": 1365, "height": 900},
#     user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
#                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
# )


        # Load cookies
        # context = browser
        with open(cookies_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await browser.add_cookies(cookies)
        page = await browser.new_page()

        # page = await context.new_page()

        # Go to TikTok upload studio
        await page.goto("https://www.tiktok.com/tiktokstudio/upload?lang=en")

        #  Directly set files on hidden input
        await page.wait_for_timeout(5000)
        file_input = await page.query_selector('input[type="file"]')
        await file_input.set_input_files(video_path)
        # Handle popup
        try:
            await page.wait_for_selector('div[role="dialog"] button:has-text("Cancel")', timeout=10000)
            await page.click('div[role="dialog"] button:has-text("Cancel")')
            print("[*] Dismissed content check popup")
        except:
            print("[*] No popup appeared")

        # Wait until video finishes processing
        # <span class="TUXText TUXText--tiktok-sans" style="color: inherit; font-size: inherit; margin-left: 4px;">Uploaded（607.82KB）</span>
        # await page.wait_for_selector("text=Video uploaded", timeout=120000)
        await page.wait_for_selector('text=Uploaded', timeout=120000)


        # Add description
        # <div class="jsx-1601248207 caption-markup"><div class="jsx-1601248207 caption-editor"><div class="DraftEditor-root DraftEditor-alignLeft"><div class="DraftEditor-editorContainer"><div aria-autocomplete="list" aria-expanded="false" class="notranslate public-DraftEditor-content" contenteditable="true" role="combobox" spellcheck="false" style="outline: none; user-select: text; white-space: pre-wrap; overflow-wrap: break-word;"><div data-contents="true"><div class="" data-block="true" data-editor="6kko1" data-offset-key="dn0nv-0-0"><div data-offset-key="dn0nv-0-0" class="public-DraftStyleDefault-block public-DraftStyleDefault-ltr"><span data-offset-key="dn0nv-0-0"><span data-text="true">linux_tutorial</span></span></div></div></div></div></div></div></div><div class="jsx-1601248207 caption-toolbar"><div class="jsx-1601248207 operation-button"><div class="jsx-1601248207 button-item"><button type="button" aria-label="Hashtag" id="web-creation-caption-hashtag-button" class="jsx-1601248207 caption-operation-icon"><svg fill="currentColor" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" width="1em" height="1em"><path d="M34.7 3.11h2.42c.54 0 .94.5.83 1.02L35.73 15h6.55c.55 0 .95.5.83 1.03l-.43 2c-.12.57-.62.97-1.2.97H34.9l-1.83 9h7c.54 0 .94.5.83 1.03l-.44 2c-.12.57-.62.97-1.2.97h-7.01L30 43.02c-.12.57-.62.98-1.2.98h-2.43a.85.85 0 0 1-.83-1.02L27.8 32H16.43l-2.25 11.02c-.12.57-.62.98-1.2.98h-2.44a.85.85 0 0 1-.83-1.02L11.95 32H5.1a.85.85 0 0 1-.83-1.03l.43-2c.13-.57.63-.97 1.2-.97h6.87l1.84-9H7.48a.85.85 0 0 1-.83-1.03l.43-2c.12-.57.62-.97 1.2-.97h7.14l2.23-10.9c.12-.58.62-.99 1.2-.99h2.44c.53 0 .94.5.83 1.02L19.9 15h11.37l2.22-10.9c.12-.58.63-.99 1.21-.99ZM19.08 19l-1.84 9h11.37l1.84-9H19.08Z"></path></svg><span class="jsx-1601248207 caption-operation-icon__text">Hashtags</span></button></div><div class="jsx-1601248207 button-item"><button type="button" aria-label="@mention" id="web-creation-caption-mention-button" class="jsx-1601248207 caption-operation-icon"><svg fill="currentColor" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" width="1em" height="1em"><path d="M24.28 44.54c-4.32 0-8.1-.87-11.33-2.6a18.05 18.05 0 0 1-7.49-7.2A21.94 21.94 0 0 1 2.87 23.9c0-4.04.87-7.57 2.6-10.61a18.21 18.21 0 0 1 7.43-7.15c3.2-1.7 6.88-2.55 11.04-2.55 4.04 0 7.59.77 10.66 2.3 3.1 1.51 5.5 3.67 7.2 6.49a18.19 18.19 0 0 1 2.6 9.79c0 3.52-.82 6.4-2.46 8.64-1.63 2.2-3.93 3.31-6.9 3.31-1.86 0-3.34-.4-4.42-1.2a4.6 4.6 0 0 1-1.73-3.7l.67.3a6.42 6.42 0 0 1-2.64 3.4 8.28 8.28 0 0 1-4.56 1.2 8.52 8.52 0 0 1-7.97-4.75 11.24 11.24 0 0 1-1.15-5.19c0-1.95.37-3.66 1.1-5.13a8.52 8.52 0 0 1 7.92-4.75c1.8 0 3.3.41 4.52 1.24 1.24.8 2.1 1.94 2.54 3.41l-.67.82v-4.04a1 1 0 0 1 1-1h2.27a1 1 0 0 1 1 1v12.05c0 .87.22 1.5.67 1.92.48.39 1.12.58 1.92.58 1.38 0 2.45-.75 3.22-2.26.8-1.53 1.2-3.44 1.2-5.7 0-3.05-.67-5.69-2.02-7.93a12.98 12.98 0 0 0-5.52-5.13 17.94 17.94 0 0 0-8.3-1.83c-3.3 0-6.23.69-8.79 2.07a14.82 14.82 0 0 0-5.9 5.76 17.02 17.02 0 0 0-2.11 8.59c0 3.39.7 6.35 2.11 8.88 1.4 2.5 3.4 4.41 6 5.76a19.66 19.66 0 0 0 9.17 2.01h10.09a1 1 0 0 1 1 1v2.04a1 1 0 0 1-1 1H24.28Zm-1-14.12c1.72 0 3.08-.56 4.07-1.68 1.03-1.12 1.54-2.64 1.54-4.56 0-1.92-.51-3.44-1.54-4.56a5.17 5.17 0 0 0-4.08-1.68c-1.7 0-3.05.56-4.08 1.68-.99 1.12-1.49 2.64-1.49 4.56 0 1.92.5 3.44 1.5 4.56a5.26 5.26 0 0 0 4.07 1.68Z"></path></svg><span class="jsx-1601248207 caption-operation-icon__text">Mention</span></button></div></div><div class="jsx-1601248207 word-count"><span class="jsx-1601248207">14</span><span class="jsx-1601248207">/</span><span class="jsx-1601248207">4000</span></div></div></div>
        # await page.fill('div[contenteditable="true"]', description)
        desc_box = await page.wait_for_selector('div[contenteditable="true"]')
        await desc_box.click()
        await desc_box.fill("")  # clear old text if needed (works in new PW)
        await desc_box.type(description, delay=50)  # delay makes it more human-like


        # Click Post button
        # <button role="button" type="button" class="Button__root Button__root--shape-default Button__root--size-large Button__root--type-primary Button__root--loading-false" aria-disabled="false" data-icon-only="false" data-size="large" data-loading="false" data-disabled="false" data-e2e="post_video_button" style="width: 200px;"><div class="Button__spinnerBox Button__spinnerBox--shape-default Button__spinnerBox--size-large Button__spinnerBox--type-primary Button__spinnerBox--loading-false"><span role="img" class="px-icon Button__spinner Button__spinner--shape-default Button__spinner--size-large Button__spinner--type-primary Button__spinner--loading-false" data-icon="Loading" data-testid="Loading"><svg width="18" height="18" fill="currentColor" will-change="auto" transform="rotate(0)" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-dasharray="49 50"></circle></svg></span></div><div class="Button__content Button__content--shape-default Button__content--size-large Button__content--type-primary Button__content--loading-false">Post</div></button>
        # await page.click('button:has-text("Post")')
        await page.click('button[data-e2e="post_video_button"]')

        # Handle "Continue to post?" popup if it appears
        await page.wait_for_timeout(5000)
        # <button class="TUXButton TUXButton--default TUXButton--medium TUXButton--primary" aria-disabled="false" type="button"><div class="TUXButton-content"><div class="TUXButton-label">Post now</div></div></button>
        # Click the main Post button
        # await page.click('button[data-e2e="post_video_button"]')

        # Handle "Continue to post?" modal if it shows up
        # Try strict button selector first
        try:
            # Preferred: button with label "Post now"
            await page.locator('button:has(.TUXButton-label:has-text("Post now"))').click()
        except:
            # Fallback: any element with "Post now" text
            try:
                await page.locator('text=Post now').click()
            except:
                pass



        # await page.click('button:has-text("Post now")')
        # await page.wait_for_timeout(10000)
        # print("Popup detected → clicked 'Post now'")
        # try:
        #     # Wait for the modal
        #     popup = await page.wait_for_selector('div:has-text("Continue to post?")', timeout=5000)
        #     if popup:
        #         # Click the "Post now" button inside the modal
        #         await page.click('button:has-text("Post now")')
        #         print("Popup detected → clicked 'Post now'")
        # except:
        #     print("No popup detected, continuing...")



        # await page.wait_for_timeout(120000)
        # input("enter:")
        await browser.close()




async def post_now(cookies_path):
    print("in get url")
    time.sleep(16)
    print("strart running get url")
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="/tmp/playwright",   # persistent profile storage
            executable_path="/usr/bin/google-chrome-stable",  # or "chromium"
            headless=True,
            locale="en-US",
            viewport={"width": 1365, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--enable-features=VaapiVideoDecoder",  # optional
            ]
        )

        page = await browser.new_page()

        # Load cookies for logged-in session
        with open(cookies_path, "r") as f:
            cookies = json.load(f)
        await page.context.add_cookies(cookies)

        url = "https://www.tiktok.com/tiktokstudio/content"
        await page.goto(url)
        await page.wait_for_timeout(5000)

        try:
            # Wait until "Post now" button is visible
            username = "shiky124"
            await page.wait_for_selector('a[href^="/@' + "shiky124" + '/video/"]')
            links = await page.query_selector_all(f'a[href^="/@{username}/video/"]')
            if not links:
                print("No video links found")
                await browser.close()
                return None

            # Get the last one
            last_link = await links[0].get_attribute("href")
            print(" Last video link:", "https://www.tiktok.com" + last_link)
            vid = last_link.replace(f"@{username}/video/","").replace("/","")
            print(" Last video id:",  vid )

            # await page.wait_for_selector("button:has-text('@shiky124/video')", timeout=20000)
            # await page.click("button:has-text('Post now')")
            print(" Clicked 'Post now' successfully")
            return vid
        except Exception as e:
            print("Could not find/click 'Post now':", e)

        await page.wait_for_timeout(5000)
        # input("Enter: ")
        await browser.close()



async def scrape_comments(my_video_id, cookies_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="/tmp/playwright",
            executable_path="/usr/bin/google-chrome-stable",
            headless=True,
            locale="en-US",
            viewport={"width": 1365, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--enable-features=VaapiVideoDecoder",
            ]
        )
        page = await browser.new_page()

        # Load cookies
        with open(cookies_path, "r") as f:
            cookies = json.load(f)
        await page.context.add_cookies(cookies)

        url = "https://www.tiktok.com/tiktokstudio/comment/" + my_video_id
        

        results = []
        retries = 5   # try 5 times max
        while not results :
            await page.goto(url)
            await page.wait_for_timeout(3000)  # wait for comments to load
            comments = await page.query_selector_all('[data-tt="components_CommentDetail_FlexColumn_5"]')

            for comment in comments:
                username = await comment.query_selector('button span.TUXText')
                username_text = await username.inner_text() if username else None

                content = await comment.query_selector('[data-tt="components_TUXTextWithMention_TUXText"]')
                content_text = await content.inner_text() if content else None

                if username_text and "shiky124" in username_text:
                    results.append({
                        "username": username_text,
                        "comment": content_text
                    })
                    print(content_text)

            retries -= 1
        print(results)

        await browser.close()
        return results



        # # Wait for the comment container
        # await page.wait_for_selector('div[data-tt="Comment_VideoCommentPage_Container"]')

        # # Scroll a bit (TikTok uses infinite scroll for comments)
        # for _ in range(5):
        #     await page.mouse.wheel(0, 2000)
        #     await asyncio.sleep(2)

        # # Extract comments
        # comments = await page.locator(
        #     'div[data-tt="components_CommentDetail_Container"] span.TUXText'
        # ).all_inner_texts()

        # authors = await page.locator(
        #     'div[data-tt="components_NameWithIcon_TUXText"]'
        # ).all_inner_texts()

        # for author, comment in zip(authors, comments):
        #     print(f"{author}: {comment}")

        # input("ENter:")

        await browser.close()




def shell_main():
    stop_me = True
    cmd = "id"
    outfile ="linux_tutorial.mp4"
    cookies = "cookies.json"

    # Determine output to display
    out_text = run_command_capture_output(cmd)

    narration = build_enhanced_narration(cmd, out_text)
    make_video(cmd, out_text, narration, outfile=outfile)
    print(f"Enhanced tutorial video created: {outfile}")
    # upload video
    description = "we will learn about command "+cmd + " in this video tutorial"

    asyncio.run(upload_video(outfile, description, cookies))
    # get the last video url
    # time.sleep((3*60)+50)
    video_id = asyncio.run(post_now(cookies))
    print (video_id)

    # read comments

    comments = asyncio.run(scrape_comments(video_id, cookies))
    comments = comments[0]["comment"]
    print(comments)
    cmd = comments
    while stop_me:
        # Determine output to display
        out_text = run_command_capture_output(cmd)

        narration = build_enhanced_narration(cmd, out_text)
        make_video(cmd, out_text, narration, outfile=outfile)
        print(f"Enhanced tutorial video created: {outfile}")
        # upload video
        description = "we will learn about command "+cmd + " in this video tutorial"

        asyncio.run(upload_video(outfile, description, cookies))
        # get the last video url
        
        video_id = asyncio.run(post_now(cookies))
        print (video_id)

        # read comments

        comments = asyncio.run(scrape_comments(video_id, cookies))
        comments = comments[0]["comment"]
        print(comments)
        cmd = comments
        if "stop_me" in comments:
            stop_me = False
    

if __name__ == "__main__":
    shell_main()
    # cookies = "cookies.json"
    # video_id = "7544332061788605704"
    # comments = asyncio.run(scrape_comments(video_id, cookies))
    # comments = comments[0]["comment"]
    # print(comments)
