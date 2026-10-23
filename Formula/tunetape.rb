# Homebrew formula for tunetape.
#
# Publishing checklist (each release):
#   1. Bump the version in pyproject.toml, commit, and push.
#   2. Cut a GitHub release/tag (e.g. v0.4.0). Then update `url` below and run:
#        curl -fsSL https://github.com/oauramos/tunetape/archive/refs/tags/v0.4.0.tar.gz | shasum -a 256
#      and paste the digest into `sha256`.
#   3. Refresh the resource blocks if textual's dependency tree changed:
#        brew update-python-resources Formula/tunetape.rb
#   4. Copy this file into the tap repo (github.com/oauramos/homebrew-tunetape,
#      under Formula/) so `brew install oauramos/tunetape/tunetape` works.
#
# Local test before publishing:
#   brew install --build-from-source ./Formula/tunetape.rb
#   brew audit --strict --new Formula/tunetape.rb
#   brew test tunetape
class Tunetape < Formula
  include Language::Python::Virtualenv

  desc "Terminal audio player that streams from YouTube, Spotify, and KHInsider"
  homepage "https://github.com/oauramos/tunetape"
  url "https://github.com/oauramos/tunetape/archive/refs/tags/v0.9.0.tar.gz"
  sha256 "92f6a7a7083b82cde3f15cf63aa50d6629024ac3b3524912ae9c40d38a172104"
  license "MIT"

  depends_on "mpv"          # links FFmpeg libraries — provides audio decode + dynaudnorm
  depends_on "python@3.12"
  depends_on "yt-dlp"       # required for YouTube playback

  # curl_cffi (KHInsider pages, see khinsider.py) builds from source with a
  # download step; the formula leaves it out and tunetape falls back to the
  # system /usr/bin/curl, which KHInsider lets through.
  pypi_packages exclude_packages: %w[curl-cffi cffi pycparser certifi]

  resource "linkify-it-py" do
    url "https://files.pythonhosted.org/packages/45/98/7a1a5f31fd5c7ba93e963b168e244b8e3dd705b3d2a718e3c3307583bf57/linkify_it_py-2.2.0.tar.gz"
    sha256 "907acd2d17ac1fbb9ddb62c8957ccbd6158cac602231a15c3b0cd1e215f03cee"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdit-py-plugins" do
    url "https://files.pythonhosted.org/packages/59/fc/f8d0863f8862f25602c0404d75568e89fb6b4109804645e5cdfb1be5cf56/mdit_py_plugins-0.6.1.tar.gz"
    sha256 "a2bca0f039f39dbd35fb74ae1b5f998608c437463371f0ff7f49a19a17a114d0"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "platformdirs" do
    url "https://files.pythonhosted.org/packages/58/e6/d0abbb256b44e6a05748c6942511ff67db8dbb068de0c35b24bf1b395b09/platformdirs-4.13.5.tar.gz"
    sha256 "05831adf87d392760f4b5c3a0a95579ab5a10b18b9b9bd43680dcb47544788a0"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/49/2e/ced460408999b33da6b31b0021b0f37d329e202d4169aeb164493778f25b/pygments-2.21.0.tar.gz"
    sha256 "610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/e9/67/cae617f1351490c25a4b8ac3b8b63a4dda609295d8222bad12242dfdc629/rich-14.3.4.tar.gz"
    sha256 "817e02727f2b25b40ef56f5aa2217f400c8489f79ca8f46ea2b70dd5e14558a9"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/00/21/39a76b01bd5eea82a04baaca7580e105d8c59450df03998345bb2cfb307b/textual-8.2.8.tar.gz"
    sha256 "3f106a9fbc73e39dd266c9712432087de78a6d644084c7c241d6a25c3169115b"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "tunetape #{version}", shell_output("#{bin}/tunetape --version")
  end
end
