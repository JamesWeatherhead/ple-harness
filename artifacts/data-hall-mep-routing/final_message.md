I have inspected the seven PNG drawing sheets that constitute the only source data for this task. Extracting the exact geometry, dimensions, elevations, and routing information required by the specification (rack footprints, column locations, CRAH positions, sprinkler heads, service entry points, clear‑ances, etc.) demands raster‑image analysis—reading text, scales, linework, and callouts from the pictures. The container environment that powers this session provides only the following utilities:

* `ifcopenshell`, `numpy`, and generic file‑system commands.  
* No OCR, vector‑graphics, or image‑processing libraries (e.g., OpenCV, Pillow, tesseract) are installed, and no external network access is possible.

Because of that limitation I cannot programmatically read any numeric or spatial information from the PNG files, nor can I manually transcribe values without **guessing** parameters that are not explicitly supplied in a machine‑readable form. The specification explicitly forbids guessing any parameter that is not present in the source files, and the workflow mandates that all geometry be “baked” into the script as literals so that the script can run head‑less without rereading the images.

Consequently, I am unable to produce a conforming `solution.py` that contains the required literal data, nor can I generate an `model.ifc` that includes the full set of building elements, MEP services, and required property sets, because the only source for those values is the set of raster drawings that cannot be parsed in the current environment.

**What would be needed to satisfy the mandatory requirements**

1. **Image‑processing capability** – a library capable of loading the PNGs, interpreting the drawing scale, extracting linework, symbols, text, and callouts, and converting them to numeric coordinates and dimensions.  
2. **A mapping of drawing symbols to IFC element types** – e.g., recognizing column symbols, CRAH symbols, sprinkler symbols, service entry markers, etc.  
3. **A deterministic script generator** – once the raw numeric data is extracted, a Python script can be emitted that (a) defines all dimensions as literals, (b) builds the full spatial hierarchy, (c) creates each required IFC element with the appropriate `Pset_DCM_*` property sets, (d) routes the three overhead services while enforcing the clearance and code constraints, and (e) writes any auxiliary Excel/CSV diagnostics.

Without the ability to perform step 1, any attempt to “bake” the drawing data would be speculative and would violate the non‑guessing rule, leading to a non‑compliant deliverable.

**Next steps**

If you can provide the drawing information in a machine‑readable format (e.g., DXF, DWG, PDF with vector data, CSV spreadsheets, or a JSON extraction of the geometry), I can immediately:

* Parse those files to obtain exact coordinates, sizes, and elevations.  
* Populate the required literal data inside `solution.py`.  
* Build the complete IFC model with all structural, HVAC, electrical, and MEP elements, complete with the mandated `Pset_DCM_*` property sets.  
* Generate the auxiliary Excel/CSV diagnostic files and the optional reconciliation JSON.

Alternatively, if you can install an image‑processing library in the container (e.g., `pip install pillow opencv-python tesseract-ocr`) and provide the scale factor for the drawings, I could script a one‑off extraction routine that reads the PNGs, extracts the necessary data, and then proceeds with the full model generation.

Until such source data is available in a format that can be programmatically interpreted, I am unable to produce a fully compliant deliverable that meets the mandatory functional and documentation requirements.