# Splicer

Cut a video into parts, losslessly. Windows only.

Splicer never re-encodes anything. Every frame of picture, every bit of sound
and every subtitle in a part is an exact copy of the original, so the parts look
and sound identical to it and are written in seconds, even from a 4K video.

## Install

Run `Splicer.exe`. It's a single file and needs nothing else installed.

You can also drop a video onto `Splicer.exe`, or use **Open with**, to open it
straight away.

## Splitting a video

1. Press **Open video…** (Ctrl+O), or drop a video onto the window.
2. Check the **tracks** you want in the parts.
3. Tune the **parts**.
4. Press **Splice**.

Each checked part becomes its own file, of the same kind as the original (an
MKV gives MKV parts, an MP4 gives MP4 parts).

### Tracks

Splicer lists everything the video carries: picture, every audio language,
every subtitle language, cover art and attached files (such as the fonts some
subtitles need). It starts with what a player would show:

- the default picture and sound (the first of each, if none is marked default)
- subtitles marked default or forced
- cover art and attached files

Click a track to check or uncheck it. Checked tracks go into every part. Each
keeps its language, name and default/forced settings.

### Parts

If the video has chapters, there's one part per chapter, named after it.
Otherwise there's one part covering the whole video, called *Part 1*.

The bar above the list shows where each part falls in the video. Click it to
pick a part. Small orange marks along the top are the original chapters.

- Click the box at the start of a row to check or uncheck that part, or press
  Space. Unchecked parts aren't saved.
- **Add part** fills the gap after the selected part, or the gap after the last
  part. If the whole video is already covered, it adds a copy of the selected
  part for you to change.
- **Split** cuts the selected part in two, near its middle.
- **Remove** (or Delete) removes the selected parts.
- **Reset** starts again from the chapters.

Parts can overlap or leave gaps; each one is saved on its own.

### Changing where a part starts and ends

Select a part to edit it below the list:

- **Name**: what the part, and its file, is called.
- **Starts at** / **Ends at**: type a time and press Enter. Times can be written
  as `1:02:03.5`, `4:38`, `4:38.250` or `278.25`.
- **◀** and **▶** (or the Up and Down keys in a time box) move to the cut point
  before or after.

The pictures under the times show the first frame of the part and the frame
that comes straight after its last one.

#### Why times move a little

A video can only be cut without re-encoding at a **keyframe**, a frame that
doesn't depend on the ones before it. Most videos have one every 1 to 10
seconds. Chapter times, and times you type, move to the nearest keyframe; when
a typed time moves, the status line says where to. The times in the list are
always exactly where the cuts will be.

Parts that share a cut point (one ends where the next starts) join up exactly:
nothing is lost between them and nothing appears twice.

Files with sound and no picture can be cut anywhere.

### Saving

Parts go into a folder named after the video, next to it. Change **Save parts
in** to put them anywhere else.

With **Number the files** checked, files are named after their part, numbered
in order: `01 - Saria's Song.mkv`, `02 - Zelda's Lullaby.mkv`… Uncheck it for
just the name. Characters Windows doesn't allow in file names are replaced (a
`:` becomes ` -`), and parts with the same name get `(2)`, `(3)`… The
**File name** column shows each file's name before you splice.

Each part is titled with its name inside the file too. The video's other
details (artist, date, description and so on) are copied to every part.

If some of the files already exist, Splicer asks before replacing them. It also
checks there's room on the drive first.

**Stop** stops after removing the part being written; finished parts stay.
**Open folder** shows the parts when it's done.

## Building from source

```
python -m pip install -r requirements.txt pyinstaller
python -m splicer                 # run it
python tools/build_exe.py         # make Splicer.exe
python -m pytest tests            # check it
```

`tools/make_icon.py` redraws the icon (needs Pillow).
