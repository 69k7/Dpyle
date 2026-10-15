# Dpyle
A tool for decompiling PyInstaller archives

When a `.dll` or `.pyd` file in `WorkFolder` is opened, Dpyle uses the bundled
Resource Hacker command-line executable to extract its resources into a
sibling `EXTRACTED_<name>` folder. The file is represented in the file tree by
that folder, with the original filename and icon. Resource Hacker's extracted
text resources (including manifests and version information) stay as `.txt`
files, icons stay as `.ico`, and clicking another extracted resource opens its
source DLL/PYD in Resource Hacker. Resource-type folders are indexed in
`WorkFolder/index.xml`. The extraction switches follow the
[Resource Hacker command-line documentation](https://www.angusj.com/resourcehacker/).
