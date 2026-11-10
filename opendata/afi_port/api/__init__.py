"""HTTP routers ported from AFI's data-management control plane.

The routers keep their AFI path suffixes and response shapes; they are
mounted under a distinct ``/api/v1/afi-data`` namespace so no OpenData
contract changes while the two planes coexist.
"""
