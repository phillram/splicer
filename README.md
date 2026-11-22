# Splicer

Cut a video into parts, losslessly. Open a video, and its chapters are already
there as parts; tune them while you watch, then press **Splice** and each part
is written to its own file.

Out of the box, Splicer never re-encodes anything. Every frame of picture,
every bit of sound and every subtitle in a part is an exact copy of the
original, so the parts look and sound identical to it and are written in
seconds, even from a 4K video. If you'd rather cut on any frame, it can
re-encode just the second or two of picture next to each cut, and nothing else:
see [Where cuts can go](#where-cuts-can-go).

- Chapters become parts, named after them; or make parts from a list of
  timestamps, like the ones in video descriptions
- Picks the default sound and subtitle tracks; choose any others
- Built-in player with sound and subtitles, full screen, and a zoomable
  timeline where you drag the cuts
- Leave stretches out of a part, or join parts, into one seamless file
- Save the sound alone, untouched: the songs out of a music video
- Undo and redo; remembers your parts for each video

Windows only. Dark themed. Splicer never goes online: everything it does
happens on your PC.

## Getting it running

Download `Splicer.exe` from the
[latest release](https://github.com/phillram/splicer/releases/latest). It's a
single file and needs nothing else installed: the video libraries it uses
(FFmpeg's, through PyAV) are inside it.

You can also drop a video onto `Splicer.exe`, or use **Open with**, to open it
straight away. Splicer keeps its settings, and the parts it remembers for each
video, in `%APPDATA%\Splicer`.

From source, Python 3.10 or newer:

```
python -m pip install -r requirements.txt
python -m splicer
```

## Splitting a video

1. Press **Open video…** (Ctrl+O), drop a video onto the window, or pick one
   you opened before from **Recent**.
2. Check the **tracks** you want in the parts.
3. Watch the video and tune the **parts**.
4. Press **Splice**.

Each checked part becomes its own file, of the same kind as the original (an
MKV gives MKV parts, an MP4 gives MP4 parts), or just its sound (see
[Saving](#saving)).

### The window

- Along the top: the video, and the settings for saving with **Splice**.
- On the left: the **Parts** and **Tracks** tabs.
- On the right: the **Player**.
- Along the bottom: the **Timeline**.

Drag the gaps between the sections to resize them. Splicer remembers the sizes.

Splicer also remembers your parts and tracks for each video. Open the same
video again and you carry on where you left off. **More ▸ Start again from the
chapters** goes back to the beginning.

### Tracks

Splicer lists everything the video carries: picture, every audio language,
every subtitle language, cover art and attached files (such as the fonts some
subtitles need). It starts with what a player would show:

- the default picture and sound (the first of each, if none is marked default)
- subtitles marked default or forced
- cover art and attached files

Click a track to check or uncheck it. Checked tracks go into every part. Each
keeps its language, name and default/forced settings. The first checked sound
track is the one you hear in the player.

### Parts

If the video has chapters, there's one part per chapter, named after it.
Otherwise there's one part covering the whole video, called *Part 1*.

- Click the box at the start of a row to check or uncheck that part. Unchecked
  parts aren't saved.
- **Add part** fills the gap after the selected part, or the gap after the last
  part. If the whole video is already covered, it adds a copy of the selected
  part for you to change.
- **Split** cuts the selected part in two at the current time (or near its
  middle, if the current time isn't inside it).
- **Join** makes the selected parts (Ctrl+click or Shift+click them in the
  list) into one part, saved as one file: see [Leaving stretches out and
  joining parts](#leaving-stretches-out-and-joining-parts).
- **Remove** (or Delete) removes the selected parts.
- **↶** and **↷** undo and redo any change to the parts (Ctrl+Z, Ctrl+Y).
- **More**:
  - **Parts from timestamps…** makes the parts from a list of times like
    `4:38 Zelda's Lullaby`, one per line, as in video descriptions. If the
    video's own description has one (videos downloaded from YouTube often
    do), it's already filled in; otherwise paste one. Each time starts a part
    that runs to the next.
  - **Copy the parts as timestamps** puts the checked parts on the clipboard
    as such a list.
  - **Start again from the chapters**.

Parts can overlap or leave gaps; each one is saved on its own.

### Changing where a part starts and ends

Select a part to edit it below the list:

- **Name**: what the part, and its file, is called.
- **Starts at** / **Ends at**: type a time and press Enter. Times can be written
  as `1:02:03.5`, `4:38`, `4:38.250` or `278.25`.
- **◀** and **▶** (or the Up and Down keys in a time box) move to the cut point
  before or after.
- **Use current time** (I and O) starts the part at the picture showing in the
  player, or ends it just after that picture.

Or drag the edge of a part along the **Timeline**.

With **Keep touching parts joined** checked, moving a cut that two parts share
(one ends where the next starts) moves both, so nothing falls between them.

After each change the player shows the part's new first frame, or its new
last frame, so you can see exactly where it cuts.

### Leaving stretches out and joining parts

A part doesn't have to be one unbroken stretch of the video. Its file can
leave bits out, with what's either side joined seamlessly, still losslessly.

To take a stretch out of the middle of a part (say 3:41 to 4:22, without the
three seconds around 4:01):

1. Select the part and set its start and end.
2. Go to the stretch you don't want in the player and press **Cut out a
   section**. A stretch of about three seconds from there is left out.
3. Change its times in the **Leaves out** row that appears (type them, use
   Up and Down, or press **Now** to use the current time), or drag its edges
   on the timeline, where it shows as a dark "left out" block.
4. Press **Play** a little before it to watch the join: the player jumps
   straight over it, as the saved file will.

**Put back** keeps that stretch after all. A part can leave out as many
stretches as you like, and its **Length** is the length of its file.

To make one file from separate bits of the video, pick their parts in the list
(Ctrl+click or Shift+click) and press **Join**: they become one part, with
everything between them left out.

With **Cuts: On keyframes**, a stretch left out starts and ends on keyframes
too, so it can't be any length you like: in a video with a keyframe every three
seconds or so, it's three seconds, or six, and so on. Under the stretch,
Splicer lists the keyframes near it, and says why when a time you type can't
be used. With **Cuts: Anywhere**, it can be any length.

### Where cuts can go

**Cuts**, along the top, has two settings.

**On keyframes (lossless)**, the setting Splicer starts with. A video can only be
cut without re-encoding at a **keyframe**, a frame that doesn't depend on the
ones before it. Most videos have one every 1 to 10 seconds. Chapter times,
typed times, timestamps and current times all move to the nearest keyframe;
when a time you gave moves, Splicer says where to. Every part is an exact copy
of the original.

**Anywhere (re-encodes near cuts)**. A cut can go on any frame. Between a cut
and the keyframe beside it (usually a second or two, at most the gap between
keyframes), the picture is re-encoded: in the same format as the original, at
the same size, with the same colors, at a quality high enough that it looks
the same. Everything else is still copied exactly. The sound is never
re-encoded: it's copied too, cut to within a fiftieth of a second, which you
can't hear. The re-encoded bits show as orange bands along the bottom of the
parts on the timeline, and the part's details say how many seconds there are.
Re-encoding 4K takes a while: a few seconds to half a minute for each cut.

Changing to **Anywhere** with the parts still as they came from the chapters
moves them to the chapters' exact times. Changing back to **On keyframes**
moves every cut to the nearest keyframe (Ctrl+Z undoes that). In Anywhere, the
**◀** and **▶** buttons, and Up and Down in a time box, step a frame at a time.

Splicer can re-encode H.264, HEVC, AV1, VP9, VP8, MPEG-2 and MPEG-4 video. For
anything else, cuts stay on keyframes.

The times in the list are always exactly where the cuts will be. Parts that
share a cut point join up exactly: nothing is lost between them and nothing
appears twice.

Files with sound and no picture can always be cut anywhere, without
re-encoding.

### Watching

Selecting a part takes the player to its start. Under the picture:

- The **bar** shows the whole video, with the parts on it. Click or drag
  anywhere to jump there. The outline shows the stretch the Timeline is showing.
- **⏮** goes to the part's first frame and **⏭** to its last.
- **⏪** and **⏩** go back or forward 5 seconds.
- **▶** plays and **⏸** pauses. Clicking the picture does the same.
- **⏹** stops, back at the start of the part.
- **‹ Frame** and **Frame ›** step one frame at a time.
- **At the end of the part** decides what happens when playing reaches the end
  of the selected part: **Stop** on its last frame, **Loop the part** from its
  start, or **Keep playing** past it.
- **Subtitles** shows a subtitle track over the picture. It starts with the
  checked default or forced one. Text subtitles (SRT, ASS, MP4 text, WebVTT)
  can be shown; picture subtitles (Blu-ray, DVD) still go into the parts, but
  can't be shown in the player.
- **Save frame** (S) saves the picture in the player, at full size, as a PNG in
  the folder the parts go to.
- The speaker button mutes; the slider beside it sets the volume.
- **Full screen** (or double-clicking the picture) fills the screen. Its
  controls show when you move the mouse. Esc or double-click leaves.

Above the picture is how far into the selected part's file the current time
is (stretches left out don't count).

If the picture can't keep up (a 4K video on a slower PC), the player skips
frames rather than let the sound stutter.

### The timeline

The timeline along the bottom shows the video with a time ruler, every part
with its name and length, the original chapters as orange marks and, once
you've zoomed in far enough, every keyframe: the only places a cut can go.

- **Drag the edge of a part**, or of a stretch left out of one, to move that
  cut. It jumps from keyframe to keyframe, and the player shows the frame
  there as you drag.
- **Click or drag** anywhere else to jump there.
- **Double-click** a part to select it.
- **Ctrl+wheel** zooms in and out around the pointer, and the **wheel** scrolls.
  The buttons zoom too: **−**, **+**, **Part** (the selected part) and **Whole
  video**. While playing, the timeline follows along.

### Keyboard

| Key | |
|---|---|
| Space | Play / pause |
| ← → | Back / forward 5 seconds |
| , . | One frame back / forward |
| Ctrl+← Ctrl+→ | The keyframe before / after |
| Home End | The part's first / last frame |
| Page Up, Page Down | The previous / next part |
| I O | Start / end the part at the current time |
| Ctrl+Z, Ctrl+Y | Undo / redo |
| + − | Zoom the timeline in / out |
| S | Save the frame as a picture |
| F or F11 | Full screen; Esc leaves |
| M | Mute |

Hover over **Keyboard shortcuts** at the bottom right to see these in the app.

### Saving

The settings for saving are along the top, under the video's name.

Parts go into a folder named after the video, next to it. Change **Save parts
in** to put them anywhere else.

Save parts **as**:

- **Video**: every checked track, in a file like the original.
- **Sound only**: just the first checked sound track, untouched, in its own
  kind of file: Opus as `.opus`, AAC as `.m4a`, MP3 as `.mp3`, FLAC as `.flac`,
  Vorbis as `.ogg`, anything else as `.mka`. The parts are numbered as the
  tracks of an album named after the video. Handy for the songs in a music
  video.

With **Number the files** checked, files are named after their part, numbered
in order: `01 - Saria's Song.mkv`, `02 - Zelda's Lullaby.mkv`… Uncheck it for
just the name. Characters Windows doesn't allow in file names are replaced (a
`:` becomes ` -`), and parts with the same name get `(2)`, `(3)`… The
**File name** column shows each file's name before you splice.

Each part is titled with its name inside the file too. The video's other
details (artist, date, description and so on) are copied to every part.

If some of the files already exist, Splicer asks before replacing them. It also
checks there's room on the drive first.

**Cancel** stops splicing after removing the part being written; finished parts
stay. **Open folder** shows the parts when it's done.

## Building Splicer.exe

```
python -m pip install -r requirements.txt pyinstaller
python tools/build_exe.py         # makes Splicer.exe, beside this README
python -m pip install pytest
python -m pytest tests            # checks it
```

`tools/make_icon.py` redraws the icon (needs Pillow).
