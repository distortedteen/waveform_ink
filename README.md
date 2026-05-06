# waveform-ink

```
             ▗▀▖             ▗    ▌  
▌  ▌▝▀▖▌ ▌▞▀▖▐  ▞▀▖▙▀▖▛▚▀▖▄▄▖▄ ▛▀▖▌▗▘
▐▐▐ ▞▀▌▐▐ ▛▀ ▜▀ ▌ ▌▌  ▌▐ ▌   ▐ ▌ ▌▛▚ 
 ▘▘ ▝▀▘ ▘ ▝▀▘▐  ▝▀ ▘  ▘▝ ▘   ▀▘▘ ▘▘ ▘                                                                                                                     
                                                                                                                        
```

```
▁▁▁▁▁▁▂▂▂▃▄▄▅▅▆▆▇███▇▆▅▄▃▂▁▁▁▁▁▁▁▁▁▁▁▁▁▂▂▃▄▅▆▇██
▁▁▁▁▁▁▁▁▁▂▂▃▃▄▅▅▆▇▇██▇▆▅▄▃▃▂▂▁▁▁▁▁▁▁▁▁▁▂▃▄▅▆▇▇██
```

## install

```bash
# all platforms
pip install numpy soundfile

# for live mic input (needs PortAudio)
# Fedora / CachyOS:
sudo dnf install portaudio-devel
pip install sounddevice

# Ubuntu / Debian:
sudo apt install libportaudio2
pip install sounddevice
```

> mp3 files need libsndfile compiled with mp3 support, or convert to wav/flac first.
> `ffmpeg -i song.mp3 song.wav` works perfectly.

## usage

```bash
# live mic
python waveform_ink.py

# audio file
python waveform_ink.py song.wav
python waveform_ink.py track.flac --style=mirror --color=amber

# pick a specific mic
python waveform_ink.py --list-devices
python waveform_ink.py --device=2

# file at 2x speed (denser waveform)
python waveform_ink.py song.wav --speed=2.0
```

## controls

| key      | action                              |
|----------|-------------------------------------|
| `SPACE`  | freeze / unfreeze frame             |
| `s`      | save frozen frame as `.txt`         |
| `g`      | cycle glyph set                     |
| `v`      | cycle render style                  |
| `c`      | cycle colormap                      |
| `+` / `-`| raise / lower gain                  |
| `r`      | reset gain to default               |
| `q / ESC`| quit                                |

## styles

| name     | look                                           |
|----------|------------------------------------------------|
| `bar`    | classic bottom-up amplitude bar (default)      |
| `mirror` | grows from both edges toward center            |
| `center` | grows from center outward — oscilloscope look  |
| `dot`    | single trace dot, moves with amplitude         |

## glyph sets

| name      | chars            | vibe              |
|-----------|------------------|-------------------|
| `blocks`  | `▁▂▃▄▅▆▇█`       | smooth, default   |
| `braille` | `⣀⣤⣶⣿`          | dense, high-res   |
| `sharp`   | `░▒▓█`           | chunky, retro     |
| `retro`   | `.:!|I#@`        | classic ASCII     |

## colormaps

`green` · `amber` · `cyan` · `ghost` · `heat`

amplitude drives color: low = base tone, mid = white, peak = accent (bold)

## how it works

```
audio source (mic or file)
    └── RMS computed per ~23ms chunk (CHUNK_SIZE=1024 @ 44100hz)
            └── gain-multiplied, clamped to 0–1
                    └── deque of up to 2000 values (scrolling history)
                            └── latest N values rendered as columns
                                    one column per terminal character cell
                                    height encoded in glyph ramp
```

**why RMS?**  peak amplitude is spiky and hard to read visually.
RMS (root mean square) gives the *energy* of the signal per window —
quiet passages read low, loud strums / bass hits read high.
It's the same metric VU meters use.

## exported .txt files

frames are saved as `waveform_YYYYMMDD_HHMMSS.txt` in the current directory.
they include a metadata header and render perfectly in any monospace font.
paste them into GitHub READMEs inside a ` ``` ` block.

## recommended terminal setup (Hyprland / kitty)

```ini
font_family  JetBrainsMono Nerd Font
font_size    8.0
background   #000000
```

smaller font = more columns = smoother waveform.
at 8pt fullscreen you get ~400 columns — beautiful resolution.
