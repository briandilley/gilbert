# Vendor-free backends in `integrations/`; every third-party integration is a std-plugin

A backend with no third-party-vendor dependency lives in core (`src/gilbert/integrations/`);
anything that pulls in a vendor SDK or hits a vendor API lives as a plugin in the `std-plugins/`
submodule. The dividing line is purely "does it have a third-party vendor dependency."

This keeps core dependency-light and makes all vendor code optional, swappable, and independently
versioned across the submodule boundary. The trade-off is that the same conceptual backend family
can be split across two repos (e.g. the local speaker in `integrations/`, Sonos in a plugin), and
"where does this backend go?" is answered by the dependency test, not by what it does.
