//! A 1-bit frame buffer.

use crate::font::Font;

/// A `W` × `H` 1-bit image. `true` means the dot shows its bright side.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Frame<const W: usize, const H: usize> {
    dots: [[bool; W]; H],
}

impl<const W: usize, const H: usize> Default for Frame<W, H> {
    fn default() -> Self {
        Self::new()
    }
}

impl<const W: usize, const H: usize> core::fmt::Debug for Frame<W, H> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        for row in &self.dots {
            for &dot in row {
                f.write_str(if dot { "#" } else { "." })?;
            }
            f.write_str("\n")?;
        }
        Ok(())
    }
}

impl<const W: usize, const H: usize> Frame<W, H> {
    pub const WIDTH: usize = W;
    pub const HEIGHT: usize = H;

    /// An all-dark frame.
    pub const fn new() -> Self {
        Self {
            dots: [[false; W]; H],
        }
    }

    /// A frame with every dot set to `value`.
    pub const fn filled(value: bool) -> Self {
        Self {
            dots: [[value; W]; H],
        }
    }

    pub fn get(&self, x: usize, y: usize) -> bool {
        x < W && y < H && self.dots[y][x]
    }

    /// Sets one dot. Coordinates outside the frame are ignored, so text can
    /// run off the edge without panicking.
    pub fn set(&mut self, x: i32, y: i32, value: bool) {
        if (0..W as i32).contains(&x) && (0..H as i32).contains(&y) {
            self.dots[y as usize][x as usize] = value;
        }
    }

    /// Draws `text` with its top-left corner at (`x`, `y`) and returns the
    /// x coordinate just past the last glyph. Characters missing from the
    /// font are skipped. Only lit pixels are drawn, so draw onto a cleared
    /// frame.
    pub fn draw_text(&mut self, x: i32, y: i32, font: &Font, text: &str) -> i32 {
        let mut cursor = x;
        let mut first = true;
        for glyph in text.chars().filter_map(|c| font.glyph(c)) {
            if !first {
                cursor += font.spacing as i32;
            }
            first = false;
            for (row, bits) in glyph.rows.iter().enumerate() {
                for col in 0..glyph.width {
                    let lit = bits & (1 << (glyph.width - 1 - col)) != 0;
                    if lit {
                        self.set(cursor + col as i32, y + row as i32, true);
                    }
                }
            }
            cursor += glyph.width as i32;
        }
        cursor
    }

    /// Draws `text` horizontally centred at row `y`. Returns the column after
    /// its last glyph.
    pub fn draw_text_centered(&mut self, y: i32, font: &Font, text: &str) -> i32 {
        let width = font.text_width(text) as i32;
        let x = (W as i32 - width) / 2;
        self.draw_text(x, y, font, text)
    }

    /// All dots whose value differs between `self` and `other`, as
    /// `(x, y, value_in_other)`, row by row.
    pub fn diff<'a>(&'a self, other: &'a Self) -> impl Iterator<Item = (usize, usize, bool)> + 'a {
        (0..H).flat_map(move |y| {
            (0..W).filter_map(move |x| {
                let new = other.dots[y][x];
                (self.dots[y][x] != new).then_some((x, y, new))
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::font::FONT_3X5;
    use std::format;

    #[test]
    fn text_is_drawn_and_clipped() {
        let mut f: Frame<6, 5> = Frame::new();
        let end = f.draw_text(0, 0, &FONT_3X5, "11");
        assert_eq!(end, 7);
        assert_eq!(format!("{f:?}"), ".#...#\n##..##\n.#...#\n.#...#\n###.##\n");
    }

    #[test]
    fn out_of_range_set_is_ignored() {
        let mut f: Frame<2, 2> = Frame::new();
        f.set(-1, 0, true);
        f.set(2, 0, true);
        f.set(0, 5, true);
        assert_eq!(f, Frame::new());
    }

    #[test]
    fn diff_lists_only_changed_dots() {
        let a: Frame<3, 2> = Frame::new();
        let mut b = a;
        b.set(2, 1, true);
        b.set(0, 0, true);
        let d: std::vec::Vec<_> = a.diff(&b).collect();
        assert_eq!(d, [(0, 0, true), (2, 1, true)]);
        assert_eq!(b.diff(&b).count(), 0);
    }
}
