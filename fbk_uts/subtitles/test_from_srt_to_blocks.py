# Copyright 2023 FBK

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License
import unittest

from examples.speech_to_text.scripts import from_srt_to_blocks


class TestDoctest(unittest.TestCase):
    def test_doctest(self):
        import doctest
        results = doctest.testmod(m=from_srt_to_blocks)
        self.assertEqual(0, results.failed)


if __name__ == '__main__':
    unittest.main()
