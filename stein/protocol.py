# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio

from asyncio.streams import FlowControlMixin

try:
    from http_parser.parser import HttpParser
except ImportError:
    from http_parser.pyparser import HttpParser


class HTTPProtocol(FlowControlMixin, asyncio.Protocol):

    def __init__(self, stream_reader, callback, loop=None):
        super().__init__(loop=loop)
        self._stream_reader = stream_reader
        self._stream_writer = None

        self._callback = callback
        self._task = None

        self._server = None

    def connection_made(self, transport):
        self._parser = HttpParser()

        self._stream_reader.set_transport(transport)
        self._stream_writer = asyncio.StreamWriter(
            transport,
            self,
            self._stream_reader,
            self._loop,
        )

        # Grab the name of our socket if we have it
        self._server = transport.get_extra_info("sockname")

    def connection_lost(self, exc):
        if exc is None:
            self._stream_reader.feed_eof()
        else:
            self._stream_reader.set_exception(exc)

        super().connection_lost(exc)

    def data_received(self, data):
        # Parse our incoming data with our HTTP parser
        self._parser.execute(data, len(data))

        # If we have not already handled the headers and we've gotten all of
        # them, then invoke the callback with the headers in them.
        if self._task is None and self._parser.is_headers_complete():
            coro = self.dispatch(
                {
                    "server": self._server,
                    "protocol": b"HTTP/" + b".".join(
                        str(x).encode("ascii")
                        for x in self._parser.get_version()
                    ),
                    "method": self._parser.get_method().encode("latin1"),
                    "path": self._parser.get_path().encode("latin1"),
                    "query": self._parser.get_query_string().encode("latin1"),
                    "headers": self._parser.get_headers(),
                },
                self._stream_reader,
                self._stream_writer,
            )
            self._task = asyncio.Task(coro, loop=self._loop)

        # Determine if we have any data in the body buffer and if so feed it
        # to our StreamReader
        if self._parser.is_partial_body():
            self._stream_reader.feed_data(self._parser.recv_body())

        # Determine if we've completed the end of the HTTP request, if we have
        # then we should close our stream reader because there is nothing more
        # to read.
        if self._parser.is_message_complete():
            self._stream_reader.feed_eof()

    def eof_received(self):
        # We've gotten an EOF from the client, so we'll propagate this to our
        # StreamReader
        self._stream_reader.feed_eof()

    @asyncio.coroutine
    def dispatch(self, request, request_body, response):
        # Get the status, headers, and body from the callback. The body must
        # be iterable, and each item can either be a bytes object, or an
        # asyncio coroutine, in which case we'll ``yield from`` on it to wait
        # for it's value.
        status, resp_headers, body = yield from self._callback(
            request,
            request_body,
        )

        # Write out the status line to the client for this request
        # TODO: We probably don't want to hard code HTTP/1.1 here
        response.write(b"HTTP/1.1 " + status + b"\r\n")

        # Write out the headers, taking special care to ensure that any
        # mandatory headers are added.
        # TODO: We need to handle some required headers
        for key, values in resp_headers.items():
            # In order to handle headers which need to have multiple values
            # like Set-Cookie, we allow the value of the header to be an
            # iterable instead of a bytes object, in which case we'll write
            # multiple header lines for this header.
            if isinstance(values, (bytes, bytearray)):
                values = [values]

            for value in values:
                response.write(key + b": " + value + b"\r\n")

        # Before we get to the body, we need to write a blank line to separate
        # the headers and the response body
        response.write(b"\r\n")

        for chunk in body:
            # If the chunk is a coroutine, then we want to wait for the result
            # before we write it.
            if asyncio.iscoroutine(chunk):
                chunk = yield from chunk

            # Write our chunk out to the connect client
            response.write(chunk)

        # We've written everything in our iterator, so we want to close the
        # connection.
        response.close()


class HTTPServer:

    protocol_class = HTTPProtocol

    def __init__(self, callback, loop=None):
        self.callback = callback
        self.loop = None

    def __call__(self):
        reader = asyncio.StreamReader(loop=self.loop)
        protocol = self.protocol_class(reader, self.callback, loop=self.loop)
        return protocol
