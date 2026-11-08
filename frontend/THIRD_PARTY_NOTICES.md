# Third-party notices

## shadcn/ui

The button and dialog composition in `src/components/ui/` is adapted from the official shadcn/ui new-york-v4 registry, retrieved 5 November 2026:

- https://ui.shadcn.com/r/styles/new-york-v4/button.json
- https://ui.shadcn.com/r/styles/new-york-v4/dialog.json
- https://github.com/shadcn-ui/ui/blob/main/LICENSE.md

Changes: semantic project CSS replaces Tailwind utility strings; selective Radix packages replace the aggregate package; unused sizes/icons/exports are omitted. Existing application wrappers retain locale, safe dismissal and pending semantics. This is component source adaptation, not use of a third-party application template. Upstream snapshots and hashes are recorded in `local/fe-base-03/registry-proof.json` for engineering review.

MIT License

Copyright (c) 2023 shadcn

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

## Dependencies

Installed dependencies retain their own license files in their packages. Direct additions for these components: @radix-ui/react-dialog, @radix-ui/react-slot and clsx (MIT), class-variance-authority (Apache-2.0). The build copies this notice and dependency license/notice texts into the public distribution. No paid registry or template is included.
