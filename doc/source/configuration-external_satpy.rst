Configuring External Satpy Components
-------------------------------------

Replacing Satpy by External Installation
========================================

MTG-SIFT can be instructed to import Satpy modules from another location than
from the site packages of the active Python environment when the following
setting points to an appropriate package directory::

   satpy_import_path: [directory path]

For example you can use your development version of Satpy cloned directly from
GitHub to ``/home/me/development/satpy`` by configuring::

   satpy_import_path: "/home/me/development/satpy/satpy"

or setting the according environment variable before starting MTG-SIFT::

   export UWSIFT_SATPY_IMPORT_PATH="/home/me/development/satpy/satpy"

It is your responsibility to make sure the setting points to a suitable Satpy
package: If the given path doesn't point to a Python package directory or not to
one providing Satpy, the application may exit immediately throwing Exceptions.

Using Extra Readers
===================

Several data formats which are or will be produced by EUMETSAT need special
readers which are not (yet) part of the official Satpy distribution. EUMETSAT
maintains a Git repository ``satpy/local_readers`` on their `GitLab
<https://gitlab.eumetsat.int/satpy/local_readers>`_ providing these special
readers. To use these readers in addition to those included in Satpy the path to
the root directory of a clone of this repository must be configured via the
following setting first::

    satpy_extra_readers_import_path: [directory path]

Furthermore the desired readers need to be added to the configuration
``data_reading.readers`` and their reader specific configuration as well (see
**TODO**).

For example assuming that the repository has been cloned as follows::

    git clone https://gitlab.eumetsat.int/satpy/local_readers.git /path/to/clone/of/satpy/local_readers

the readers for the *FCI L1 Landmark Locations Catalogue*, *FCI L1 GEOOBS
Landmarks* (landmark locations) and *FCI L1 GEOOBS Landmark Matching Results*
(landmark navigation error) can be made available in MTG-SIFT with::

    satpy_extra_readers_import_path: /path/to/clone/of/satpy/local_readers

    data_reading:
      readers:
        ...
        - fci_l1_cat_lmk_loc
        - fci_l1_geoobs_lmk_loc
        - fci_l1_geoobs_lmk_nav_err
        ...

and adding according reader detail configuration files
``~/.config/SIFT/settings/config/readers/fci_l1_cat_lmk_loc.yaml``,
``~/.config/SIFT/settings/config/readers/fci_l1_geoobs_lmk_loc.yaml`` and
``~/.config/SIFT/settings/config/readers/fci_l1_geoobs_lmk_nav_err.yaml``.
