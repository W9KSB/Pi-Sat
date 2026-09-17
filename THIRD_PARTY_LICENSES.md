# Third-Party Licenses

## slowrx.rs 0.5.3

Pi-Sat uses `slowrx.rs`, a Rust port of `slowrx`, as its SSTV decoding engine.

MIT License

Copyright (c) 2026 Jason Herald

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## slowrx

`slowrx.rs` is based on `slowrx`, the SSTV decoder by Oona Räisänen
(OH2EIQ). Its VIS detection, mode specifications, frequency-to-pixel mappings,
sync correlation, and other algorithmic work are derived from the original.

Copyright (c) 2007-2013, Oona Räisänen (OH2EIQ [at] sral.fi)

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

Project links:

- <https://github.com/jasonherald/slowrx.rs>
- <https://github.com/windytan/slowrx>
- <https://windytan.github.io/slowrx/>

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
