# Transcriber

Transcribe, summarize and organize transcripts locally with ease.

You can get windows and macos installers from the releases.

My setup (where this software kinda works):
- Win 11 + RTX 4070 12GB VRAM + 64 GB ddr5 RAM 6000mhz (Primary machine)
- MacBook Air M4 16 GB RAM, 256 GB ssd (Installer available, but less tested)

# Models

Everything runs locally; the app downloads the models on first use.

- **Speech-to-text**: `faster-whisper large-v3` (~3 GB).
- **Assistant LLM** (summaries, action items): **Qwen3.5 9B** Q5_K_M (~6.6 GB) — the one built-in model, no switching. Fits fully into a 12 GB GPU, so it's fast.
- **GPU acceleration** (optional, NVIDIA): the CUDA build of the local llama.cpp runtime (~460 MB), offered in Settings.

# Main Idea

As a person that simultaneously handle several projects, I like to take the most of the all condacted & participated meetings.
Drop a recording and the pipeline runs on its own: transcript, then summary (with the key facts and decisions), then action items,
then a single-file meeting report (export) with all extracted artifacts for sharing and manual analysis — everything
viewable right in the app, with on-demand screenshots per action item.

Also, all the results should be concidered as a preprocessing, with the following manual handling. That's why I also don't have any
external integrations. Apply your own brain to your data sometimes is good.

There are another cool solutions, like [vexa.ai](https://vexa.ai/). Please check it out too. 
My main point is to have a local first desktop app. Don't care about any setups, run LM Studio, connect Cloud LLM's and so on.
Just single app, you install it, and it works locally on your machine.

# Purpose

I'm trying to follow [KISS](https://en.wikipedia.org/wiki/KISS_principle) and [YAGNI](https://en.wikipedia.org/wiki/You_aren%27t_gonna_need_it) as much aas possible.
So this is single user and local first soultion. 
This is a project that I create and use myself. 
So I test and use everything on my own environemnts, and as for now don't focus on others.