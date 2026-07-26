I'd like to set up a new theme for Zed - the goal is to have a higher contrast theme, which incorporates a lot more colour, than the default zed theme. Eventually there will be two modes, but for now I just want to work on the dark version.

The theme extension is already setup in themes/ - with all styling setup in the json file.

To make this process easier and more reproducible, I'd like to setup a small python CLI app which handles the generation and registering of themes in Zed (or rather, just putting themes in the right place).

For the app:

- Use cyclopts for registering commands, parsing arguments etc
- Use coloraide for driving colour selctions - ensuring minimum contrast, harmonious colours etc https://facelessuser.github.io/coloraide/harmonies/
- Start with a single generator: it should be driven based on a foreground colour input and a background colour input - it should then leverage coloraide to come up with a _pretty_ and _usable_ theme for Zed
- The current scaffolding passes with pypi

The first theme I want you to set this up to work with should be:

- A near-black navy background. The same background for the editor as for the UI
- Quite light near-white pinkish default text in the editor - more agressively pink text in the UI elements
- Typically contrasting editor text settings for different text types (functions, keywords, strings etc) - but all aiming to be quite luminant

The scaffolding for the generator is all setup in __init__.py, you should make sure to fully understand coloraide before getting started. Once you're done run the script and register the theme in zed.
