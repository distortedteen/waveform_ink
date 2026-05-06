# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-05-06

### Added
- Live microphone input with real-time waveform visualization
- Audio file playback (WAV, FLAC, OGG, MP3 support)
- Four render styles: bar, mirror, center, dot
- Four glyph sets: blocks, braille, sharp, retro
- Five color schemes: green, amber, cyan, ghost, heat
- Interactive controls (freeze, save, cycle options, gain adjustment)
- CLI with customizable initial options
- Frame export to `.txt` files for README embedding

### Dependencies
- numpy >= 1.24.0
- sounddevice >= 0.4.6 (for microphone)
- soundfile >= 0.12.0 (for audio files)