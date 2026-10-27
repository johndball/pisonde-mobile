# Source and license notices

The PiSonde-specific Python files, setup scripts, tests and documentation in this public package are offered under the MIT License in LICENSE. No personal station configuration or reception records are bundled.

The separate `rate-survey/` directory is derived from Osmocom rtl-sdr. It remains under GPL version 2 or, at your option, any later version, as stated in its source headers. Its original authors, copyright notices, COPYING license and PROVENANCE.json are retained. PiSonde modifications cap the survey rate, add an offline planning diagnostic and reject excessive calculated rates. The MIT license does not override that directory's GPL license. The source needed to build this modified executable is included.

The installer downloads, rather than redistributes here, these upstream projects:

- projecthorus/horusdemodlib at 8cc43de5567ce82132d46c5e37897f9bcc09a755
- projecthorus/radiosonde_auto_rx at 53d03c72ad18ce4357c0cedd1f4acf2bf1efb36e

Their license files remain in their respective installed repositories. Distribution packages and Python dependencies keep their own licenses. Source pinning applies to those Git checkouts; OS and Python dependency resolution still depends on the installation date.
