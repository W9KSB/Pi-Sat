# Third-Party Licenses

## DireWolf

Pi-Sat optionally uses `direwolf` for APRS and AX.25 decoding. Dire Wolf is a
separate program: Pi-Sat invokes it as a child process, writes PCM to its
standard input, and reads decoded frames from its KISS TCP interface. No Dire
Wolf source is copied into or linked against Pi-Sat, and Pi-Sat does not
distribute a Dire Wolf binary.

GPL-2.0-or-later

Copyright (C) 2011-2025 John Langner, WB2OSZ

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 2 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

Project links:

- <https://github.com/wb2osz/direwolf>
