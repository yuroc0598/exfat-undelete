# exfat-undelete

Recover deleted video files from an exFAT volume (SD cards, camera cards, disk images). Single file, Python 3 standard library only, read-only on the source.

Generic carvers (PhotoRec etc.) ignore the filesystem and guess where a file ends. exFAT keeps a deleted file's name, timestamp, start cluster and **exact size** in its directory entry, so this tool reads those instead and also checks the allocation bitmap to tell you whether the file's clusters have since been reused.

## Usage

```sh
diskutil list external              # find the card, e.g. /dev/disk4
diskutil unmountDisk /dev/disk4     # unmount (don't eject)

# optional but safer: work from an image
sudo dd if=/dev/rdisk4 of=card.img bs=4m

sudo python3 exfat_undelete.py /dev/disk4                    # list only
sudo python3 exfat_undelete.py /dev/disk4 --out ~/recovered  # recover
```

Always recover to a different disk than the card. Stop using the card as soon as you notice the deletion.

| Option | Meaning |
| --- | --- |
| `--out DIR` | recover into DIR (omit to only list) |
| `--deep` | scan the whole card for deleted entries (slow; finds files from deleted folders) |
| `--carve` | also find MP4/MOV videos in free space by signature, with no directory entry needed (use after a reformat, or for files deleted long ago). Reads each file's creation date and duration, so you can spot the old ones |
| `--match TEXT` | only names containing TEXT |
| `--min-size MB` | minimum size |
| `--any` / `--ext mp4,mov` | all file types / custom extensions |
| `--offset BYTES` | byte offset of the exFAT partition, if auto-detection fails |

Status column: `intact` means none of the file's clusters were reused; `N/M clusters reused` means part of it was overwritten.

## Limits

- Reformatted cards, or files deleted long ago whose directory entries were reused: use `--carve`. It needs the file's first cluster and its `moov` section to be intact, and assumes the file was written contiguously. Only MP4/MOV (including `.m4v`/`.3gp`-style ISO files) are carved; other formats are only found through directory entries.
- Anything the card has overwritten since is gone for good, whatever the tool.
- Deleted files are assumed to be stored contiguously (cameras almost always do this). A fragmented file whose FAT chain was cleared on delete may come out corrupted.
- Partition tables: whole-volume, MBR and GPT with 512-byte sectors.
- Developed and tested on macOS only against synthetic images (`tests/`). Not yet validated across many real cards. Reports welcome.

## Tests

```sh
python3 -m unittest discover tests
```

## License

MIT
