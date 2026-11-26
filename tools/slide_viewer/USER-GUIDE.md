# EasyPath Slide Viewer

**Minimap** follows the slide's displayed rotation. The blue dot is the current center; clicking a landmark centers the main view on it. **Slide preview** opens the scanner's physical glass-slide macro or label. For OpenSlide files with a separate label, it is displayed alongside the macro. These images can contain barcodes or identifying text. They stay local and do not appear in the minimap or downloaded viewport. If no associated image can be read, the viewer reports that instead of substituting a tissue thumbnail.

**Slide/image metadata** is scrollable and groups identification, physical scale, pyramid levels, technical details and, when applicable, DICOM WSI details and calibration warnings. Missing values are marked "Not reported".

Choose **Open slides** and select one or more local WSI files. Use **Slide** or Page Up (next) and Page Down (previous) to switch. The list keeps your selection order. The viewer opens one reader at a time and remembers each slide's viewport and frame. On a stack, use **Frame** or Shift+Page Up / Shift+Page Down. Navigation stops at the ends. Zoom levels are not focal planes.

Drag to pan. Zoom with the wheel around the pointer, or use the bottom-left controls. The readout shows the EasyPath zoom level and equivalent magnification. Rotate from -180 to +180 degrees with the slider or enter an angle; **0** resets rotation. **Fit** fits the rotated slide. The minimap outlines the viewport; click or drag it to navigate. Preview and metadata are at the top left. Use the fullscreen button or F11 to enter or leave fullscreen; Escape exits it.

With calibrated spacing on both axes, the scale bar shows the physical distance along the screen, even after rotation. Select the ruler and click the start and end points, or drag between them. Ctrl-click adds intermediate points. Lengths appear beside the line in micrometers or millimeters. Right-click a line to change its color or delete it. Escape cancels an unfinished line. Completed measurements are kept per slide and frame for the current session; nothing is written to the source. Uncalibrated slides show neither a guessed length nor a scale bar.

**Download current view** saves the current rotation, zoom and margins in PNG or JPEG. Independently select **Include scale bar** and **Include measurements and lengths** to include neither, either, or both. The toolbar, minimap and unfinished measurements are never exported. Choose a new filename; existing files are never overwritten. Local image pixels may contain identifying information. The viewer does not upload anything or register slides in a database.

General editions use OpenSlide for compatible scanner files. EP, EasyPath TIFF and microscopy DICOM use the shared EasyPath readers. This General viewer does not contain proprietary scanner SDKs and is not clinically validated.
